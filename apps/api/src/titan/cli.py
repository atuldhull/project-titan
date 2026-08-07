"""The `titan` operator CLI.

Phase 35 ships ``titan memory rebuild-index`` (TITAN-BRN-004): fully reconstruct
every Qdrant collection from PostgreSQL. Phase 55 adds the ``titan benchmark``
group (Vol 1 §7): ``list`` enumerates the fixed 10-brief suite, ``run`` executes
the one executable brief end-to-end and emits a complete Section-7 KPI report.

Wiring mirrors the API process: engine + Qdrant HTTP client from
:mod:`titan.clients`, embeddings/LLM through the provider-agnostic gateway
(TITAN-BRN-006) — the CLI knows no provider SDK either.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from titan import clients
from titan.audit.service import AuditAction, build_audit_client
from titan.benchmark.egress_guard import EgressGuard, guarded_client
from titan.benchmark.kpi import KpiReport
from titan.benchmark.offline_acceptance import AcceptanceRecord, run_offline_acceptance
from titan.benchmark.runner import BenchmarkRunner
from titan.benchmark.suite import load_suite
from titan.flags.service import FlagStore, build_flag_store
from titan.identity import service as identity_service
from titan.identity import tokens
from titan.identity.tokens import ACCESS_TTL_SECONDS
from titan.incident import IncidentService, LockdownDeps
from titan.incident.service import PLAYBOOKS
from titan.llm.service import build_gateway
from titan.memory import service as memory
from titan.observability.logging import configure_logging
from titan.redaction import default_engine
from titan.redteam import RedTeamHarness
from titan.runtime.service import build_runtime
from titan.security import SecurityContext, evaluate, load_secure_default_rows
from titan.settings import Settings, get_settings
from titan.tools.allowlists import allowed_for_role
from titan.tools.sandbox.policy import SandboxPolicy

# Repo root (apps/api/src/titan/cli.py -> parents[4] == repo root) for the map file.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_COMPLIANCE_MAP = _REPO_ROOT / "security" / "compliance-map.yaml"


async def _rebuild_index() -> int:
    settings = get_settings()
    engine = clients.create_engine(settings)
    qdrant_http = clients.create_http_client(settings)
    gateway = build_gateway(settings, engine=engine)
    try:
        report = await memory.rebuild_index(engine, memory.QdrantIndex(qdrant_http), gateway)
    finally:
        await gateway.aclose()
        await qdrant_http.aclose()
        await engine.dispose()
    print(json.dumps(report.model_dump(), indent=2))
    return 0


def _benchmark_list() -> int:
    """Enumerate the fixed benchmark suite (the executable brief plus the fixtures)."""
    suite = load_suite()
    payload = {
        "version": suite.version,
        "briefs": [
            {
                "id": b.id,
                "title": b.title,
                "size": b.size,
                "local_only": b.local_only,
                "executable": b.executable,
            }
            for b in suite.briefs
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


async def _run_benchmark(brief_id: str | None, profile: str) -> KpiReport:
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    gateway = build_gateway(settings, engine=engine, redis=redis)
    try:
        runner = BenchmarkRunner(engine, gateway=gateway)
        return await runner.run(
            brief_id=brief_id,
            profile=profile,
            generated_at=dt.datetime.now(dt.UTC).isoformat(),
        )
    finally:
        await gateway.aclose()
        await redis.aclose()
        await engine.dispose()


_DB_WRITE_WARNING = (
    "benchmark run WRITES benchmark + chaos data (projects, users, messages, "
    "outbox/inbox rows, llm_usage) to the database at TITAN_DATABASE_URL and does "
    "NOT clean up. Point it at a DISPOSABLE database — never production. "
    "Re-run with --confirm once you have."
)


def _benchmark_run(brief_id: str | None, profile: str, output: str | None, confirm: bool) -> int:
    """Execute the one executable brief and emit its Section-7 KPI report. Exit 0
    iff every measured MVP threshold is met. The blocking file write stays out of
    the async body (ASYNC240).

    Refuses to run without ``--confirm``: it writes (and never cleans up) benchmark
    and chaos rows to the configured database, so the operator must acknowledge the
    target is disposable."""
    if not confirm:
        print(_DB_WRITE_WARNING, file=sys.stderr)
        return 2
    report = asyncio.run(_run_benchmark(brief_id, profile))
    rendered = json.dumps(report.model_dump(), indent=2)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report.meets_mvp else 1


async def _run_titan_bench(track: str, manifest: str, profile: str) -> dict[str, object]:
    """Run the TITAN-Bench suite through the offline LLM gateway (Vol 10 §6, EVO-016).
    B-REG cases are generated live from the current root_cause_records."""
    from titan.evolution import bench as titan_bench

    base = get_settings()
    local_models = str(Path(base.models_config_path).with_name("models.local.yaml"))
    settings = base.model_copy(
        update={"model_policy_profile": profile, "models_config_path": local_models}
    )
    guard = EgressGuard(ollama_base_url=os.environ.get("TITAN_OLLAMA_BASE_URL"))
    base_suite = titan_bench.load_bench_suite()
    tracks = None if track in ("all", "") else [track]
    engine = redis = gateway = None
    try:
        engine = clients.create_engine(settings)
        redis = clients.create_redis(settings)
        gateway = build_gateway(settings, client=guarded_client(guard), engine=engine, redis=redis)
        # Fold in live regression cases from the current Root-Cause Records (Phase 85).
        live_reg = await titan_bench.bench_cases_from_rcrs(engine)
        suite = titan_bench.BenchSuite(
            suite_version=base_suite.suite_version, cases=(*base_suite.cases, *live_reg)
        )

        async def solve(case: titan_bench.BenchCase) -> dict[str, object]:
            # B-REG is resolved against the DB, NOT the model — recurrence is a fact, not a
            # guess (a model 'answer' would let a challenger fake passing the regression track).
            if case.track == "B-REG":
                sig = str(case.answer.get("signature", ""))
                return {"recurred": await titan_bench.signature_recurred(engine, sig)}
            return await titan_bench.gateway_solve(gateway, case, model_alias="local-default")

        report = await titan_bench.run_bench_async(
            suite, solve, tracks=tracks, manifest_sha=manifest, label="challenger"
        )
        return report.to_dict()
    finally:
        if gateway is not None:
            await gateway.aclose()
        if redis is not None:
            await redis.aclose()
        if engine is not None:
            await engine.dispose()


def _bench_list() -> int:
    from titan.evolution import bench as titan_bench

    suite = titan_bench.load_bench_suite()
    print(f"TITAN-Bench suite v{suite.suite_version} ({len(suite.cases)} cases):")
    for track in titan_bench.TRACKS:
        cases = suite.by_track(track)
        print(f"  {track}: {len(cases)} case(s) — {', '.join(c.id for c in cases)}")
    return 0


def _bench_run(track: str, manifest: str, profile: str, output: str | None) -> int:
    """Run TITAN-Bench and write the scored JSON + Markdown report. Exit 0 always (the
    OFFLINE GATE — scripts/check_bench.py — decides pass/fail vs gates.yaml, not the run).
    Needs a reachable local model (Ollama) for the offline profile."""
    from titan.evolution import bench as titan_bench

    report_dict = asyncio.run(_run_titan_bench(track, manifest, profile))
    rendered = json.dumps(report_dict, indent=2)
    report = titan_bench.BenchReport.from_dict(report_dict)
    md = titan_bench.render_markdown(report)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        Path(output).with_suffix(".md").write_text(md, encoding="utf-8")
    print(rendered)
    print(md, file=sys.stderr)
    return 0


async def _run_acceptance(profile: str) -> AcceptanceRecord:
    base = get_settings()
    # Force EVERY LLM path offline: model_policy_profile drives the runtime binding
    # (roster turns), and the fully-local models config drives gateway-DIRECT calls
    # (the KPI cost shim, embeddings) whose routing.default would otherwise resolve
    # to a paid provider (the profile alone does not govern those).
    local_models = str(Path(base.models_config_path).with_name("models.local.yaml"))
    settings = base.model_copy(
        update={"model_policy_profile": profile, "models_config_path": local_models}
    )
    # Allowlist egress: only loopback + the configured Ollama host may be reached.
    guard = EgressGuard(ollama_base_url=os.environ.get("TITAN_OLLAMA_BASE_URL"))
    engine = redis = gateway = None
    try:
        engine = clients.create_engine(settings)
        redis = clients.create_redis(settings)
        gateway = build_gateway(settings, client=guarded_client(guard), engine=engine, redis=redis)
        runtime = build_runtime(settings, engine, redis=redis, llm=gateway)
        return await run_offline_acceptance(
            engine,
            runtime,
            gateway=gateway,
            profile=profile,
            egress_guard=guard,
            generated_at=dt.datetime.now(dt.UTC).isoformat(),
        )
    finally:
        if gateway is not None:
            await gateway.aclose()
        if redis is not None:
            await redis.aclose()
        if engine is not None:
            await engine.dispose()


_ACCEPTANCE_WARNING = (
    "acceptance run drives all 16 roster agents as REAL local-model turns and WRITES "
    "projects/messages to TITAN_DATABASE_URL (no cleanup). Point it at a DISPOSABLE "
    "database with Ollama running, then re-run with --confirm."
)


def _acceptance_run(profile: str, output: str | None, confirm: bool) -> int:
    """Run the TITAN-AGT-007 offline full-roster acceptance and emit its record.
    Exit 0 iff every roster role ran and no paid-provider egress occurred — the
    honest ``autonomous_app_completed`` verdict is reported, never gating the run."""
    if not confirm:
        print(_ACCEPTANCE_WARNING, file=sys.stderr)
        return 2
    record = asyncio.run(_run_acceptance(profile))
    rendered = json.dumps(record.model_dump(), indent=2, default=str)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    print(rendered)
    # Exit 0 = the acceptance HARNESS ran clean: all roster roles ran, the local
    # model was actually exercised (not a silent all-error run), and egress stayed
    # offline. It never gates on the model's own output quality.
    ok = record.all_roles_ran and record.local_model_exercised and record.egress_clean
    return 0 if ok else 1


def _docker_kill_task(task_id: str) -> int:
    """Destroy EVERY sandbox container labelled titan.task=<id> — the fleet sweep the
    per-handle Sandbox.destroy() lacks (SEC-120 targeted 'kill sandbox')."""
    import docker  # lazy: only the CLI host has the Docker SDK/socket

    client = docker.from_env()
    try:
        containers = client.containers.list(all=True, filters={"label": f"titan.task={task_id}"})
        for c in containers:
            c.remove(force=True)
        return len(containers)
    finally:
        client.close()


def _build_incident_service(engine: object, redis: object, *, actor: str) -> IncidentService:
    """Wire the real lockdown side effects (flags, token epochs, sandbox sweep, audit).
    Secret rotation + plugin disable have authenticated API endpoints and are not
    exposed to the offline CLI, so their callbacks refuse rather than half-act."""
    store = build_flag_store(engine, redis)  # type: ignore[arg-type]
    audit_client = build_audit_client(engine)  # type: ignore[arg-type]
    cutoff = int(dt.datetime.now(dt.UTC).timestamp()) + 1  # revoke everything up to now

    async def flip(key: str, enabled: bool, value: str | None) -> None:
        changes: dict[str, object] = {"enabled": enabled}
        if value is not None:
            changes["value"] = value
        await store.update(key, changes)

    async def audit(resource: str, outcome: str, detail: dict[str, object]) -> None:
        await audit_client.record(
            actor=actor,
            action=AuditAction.INCIDENT_TOGGLE,
            resource=resource,
            outcome=outcome,
            detail=detail,
        )

    async def kill(task_id: str) -> int:
        return await asyncio.to_thread(_docker_kill_task, task_id)

    async def revoke_sessions() -> None:
        await identity_service.revoke_all_sessions(engine)  # type: ignore[arg-type]

    async def _no_plugin(plugin_id: str) -> None:
        raise NotImplementedError("disable a plugin via the authenticated /plugins API")

    async def _no_rotate(name: str, value: str) -> None:
        raise NotImplementedError("rotate a secret via the authenticated /secrets API")

    return IncidentService(
        LockdownDeps(
            flip_flag=flip,
            revoke_agents=lambda: tokens.revoke_agents_before(redis, cutoff),  # type: ignore[arg-type]
            revoke_privileged=lambda: tokens.revoke_privileged_before(redis, cutoff),  # type: ignore[arg-type]
            revoke_sessions=revoke_sessions,
            revoke_family=lambda fam: tokens.revoke_family(redis, fam),  # type: ignore[arg-type]
            kill_sandboxes=kill,
            disable_plugin=_no_plugin,
            rotate_secret=_no_rotate,
            audit=audit,
        )
    )


async def _run_incident(
    actor: str, run: Callable[[IncidentService], Awaitable[dict[str, object]]]
) -> dict[str, object]:
    engine = clients.create_engine(get_settings())
    redis = clients.create_redis(get_settings())
    try:
        svc = _build_incident_service(engine, redis, actor=actor)
        return await run(svc)
    finally:
        await redis.aclose()
        await engine.dispose()


_LOCKDOWN_WARNING = (
    "incident lockdown PAUSES ALL AGENT EXECUTION, sets intake read-only, and revokes "
    "every agent + privileged token on TITAN_DATABASE_URL / TITAN_REDIS_URL. This is a "
    "containment action for a real incident. Re-run with --confirm to proceed."
)


def _incident_lockdown(reason: str, actor: str, confirm: bool) -> int:
    if not confirm:
        print(_LOCKDOWN_WARNING, file=sys.stderr)
        return 2

    async def run(svc: IncidentService) -> dict[str, object]:
        return (await svc.full_lockdown(reason=reason, actor=actor)).to_dict()

    report = asyncio.run(_run_incident(actor, run))
    print(json.dumps(report, indent=2))
    return 0 if report.get("contained") else 1


def _incident_revoke_family(family_id: str, actor: str) -> int:
    async def run(svc: IncidentService) -> dict[str, object]:
        r = await svc.revoke_token_family(family_id)
        return {"action": r.action, "target": r.target, "ok": r.ok, "detail": r.detail}

    r = asyncio.run(_run_incident(actor, run))
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


def _incident_kill_sandbox(task_id: str, actor: str) -> int:
    async def run(svc: IncidentService) -> dict[str, object]:
        r = await svc.kill_sandbox(task_id)
        return {"action": r.action, "target": r.target, "ok": r.ok, "detail": r.detail}

    r = asyncio.run(_run_incident(actor, run))
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


def _redteam_run(output: str | None) -> int:
    """Run the LLM red-team corpus against the REAL hard controls (model-independent
    tool authz + redaction). Exit non-zero on ANY unauthorized tool execution or
    secret-bearing payload, so CI blocks a release on a regression (TITAN-SEC-130)."""
    engine = default_engine()
    harness = RedTeamHarness(
        tool_authorized=allowed_for_role,
        redact=lambda text: engine.redact(text, sink="redteam"),
    )
    report = harness.run()
    rendered = json.dumps(report.to_dict(), indent=2)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report.passed else 1


def _incident_playbook(which: str) -> int:
    if which == "list":
        print(json.dumps([p.to_dict() for p in PLAYBOOKS.values()], indent=2))
        return 0
    playbook = PLAYBOOKS.get(which.upper())
    if playbook is None:
        print(f"unknown playbook '{which}'; choose one of {sorted(PLAYBOOKS)}", file=sys.stderr)
        return 2
    print(json.dumps(playbook.to_dict(), indent=2))
    return 0


_PUBLIC_BIND = {"0.0.0.0", "::", "*"}  # a listener host that binds all interfaces
# The egress forward proxy + fetch-gateway bind 0.0.0.0 BY DESIGN: they live inside
# the sandbox/container network namespace (Phase 20/23), reachable only from the
# isolated sandbox, never the public internet. Excluding them avoids a false
# positive; any OTHER *_host set to a wildcard is a real exposure (secure-default #3).
# egress_forward_host/egress_gateway_host bind 0.0.0.0 inside the container network;
# sandbox_docker_host is a CLIENT target (the Docker daemon URL the SDK connects TO),
# not a listener bind — excluding it stops a false reading (review #8).
_INTERNAL_BY_DESIGN = {"egress_forward_host", "egress_gateway_host", "sandbox_docker_host"}


def _hosts_are_internal(settings: Settings) -> bool:
    """True iff no PUBLIC-facing ``*_host`` listener binds an all-interfaces address.
    An operator who binds an exposed service to 0.0.0.0 relaxes secure-default #3."""
    for key, value in settings.model_dump().items():
        if key in _INTERNAL_BY_DESIGN:
            continue
        if key.endswith("_host") and isinstance(value, str) and value.strip() in _PUBLIC_BIND:
            return False
    return True


def _build_security_context(settings: Settings) -> SecurityContext:
    """Gather the real signals for `titan security check` from a live install. The
    flippable, Settings-backed rows are resolved dynamically (an operator relaxation
    is caught); the built-in controls (audit trigger, sandbox hardening, redaction
    wiring, memory namespacing) are asserted at their construction-time secure
    default — each check's detail states exactly what it inspects (TITAN-SEC-140)."""
    # Resolve the sandbox hardening from the REAL policy default (a code regression
    # that relaxes it is caught), not a hardcoded True (review #4/#7).
    policy = SandboxPolicy()
    sandbox_hardened = (
        policy.read_only_rootfs and policy.no_new_privileges and policy.drop_all_capabilities
    )
    return SecurityContext(
        sandbox_egress_network=settings.sandbox_egress_network,
        auto_deploy_enabled=settings.auto_deploy_enabled,
        auto_merge_enabled=settings.auto_merge_enabled,
        require_tls=settings.require_tls,
        hsts_enabled=settings.hsts_enabled,
        rate_limit_enabled=settings.rate_limit_enabled,
        access_ttl_seconds=ACCESS_TTL_SECONDS,
        internal_binding=_hosts_are_internal(settings),
        # Settings/policy-backed rows resolved dynamically (an operator relaxation or a
        # code regression is caught); the rest stay at the asserted construction-time
        # default (see each check's detail + the help text) (review #4/#7).
        sandbox_hardened=sandbox_hardened,
        vault_initialized=bool(settings.vault_age_key),
        budget_caps_configured=bool(settings.prices_config_path),
        error_detail_suppressed=not settings.debug,
    )


def _security_check(output: str | None) -> int:
    """Check the §15 secure-defaults checklist against this install (TITAN-SEC-140).
    Prints a scorecard and exits non-zero on ANY failing row. The Settings/policy-
    backed rows are VERIFIED dynamically (an operator relaxation is caught); the
    remaining rows are ASSERTED construction-time invariants, labelled as such in the
    output so the scorecard never overstates what was inspected (review #4/#7)."""
    rows = load_secure_default_rows(_COMPLIANCE_MAP)
    if len(rows) != 15:
        print(
            f"security check: expected 15 secure-default rows, found {len(rows)} in "
            f"{_COMPLIANCE_MAP} — the checklist is incomplete",
            file=sys.stderr,
        )
        return 2
    card = evaluate(_build_security_context(get_settings()), rows)
    rendered = json.dumps(card.to_dict(), indent=2)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    for r in card.results:
        mark = "PASS" if r.status.value == "pass" else "FAIL"
        print(f"[{mark}] {r.row_id:>2}. {r.name} ({', '.join(r.ref)}) — {r.detail}")
    print(f"\nsecure-defaults: {len(card.results) - len(card.failures)}/{len(card.results)} pass")
    return 0 if card.passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="titan", description="TITAN operator CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)

    memory_cmd = subcommands.add_parser("memory", help="Memory Service maintenance (Vol 5)")
    memory_sub = memory_cmd.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser(
        "rebuild-index",
        help="Reconstruct all Qdrant collections from PostgreSQL (TITAN-BRN-004)",
    )

    bench_cmd = subcommands.add_parser("benchmark", help="Section-7 KPI benchmark suite (Vol 1 §7)")
    bench_sub = bench_cmd.add_subparsers(dest="benchmark_command", required=True)
    bench_sub.add_parser("list", help="List the fixed 10-brief benchmark suite")
    run_cmd = bench_sub.add_parser(
        "run",
        help="Run the executable brief and emit a KPI report (WRITES to TITAN_DATABASE_URL)",
    )
    run_cmd.add_argument(
        "--brief", default=None, help="Brief id to run (default: the executable brief)"
    )
    run_cmd.add_argument("--profile", default="mock", help="Run profile label (mock|cloud|local)")
    run_cmd.add_argument("--output", default=None, help="Write the KPI report JSON to this path")
    run_cmd.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge that the run writes benchmark/chaos data to a DISPOSABLE database",
    )

    tb_cmd = subcommands.add_parser("bench", help="TITAN-Bench agent-performance suite (Vol 10 §6)")
    tb_sub = tb_cmd.add_subparsers(dest="bench_command", required=True)
    tb_sub.add_parser("list", help="List the TITAN-Bench tracks + cases")
    tb_run = tb_sub.add_parser("run", help="Run TITAN-Bench and emit a scored JSON+MD report")
    tb_run.add_argument("--track", default="all", help="Track to run (B-CODE|...|all)")
    tb_run.add_argument("--manifest", default="", help="Manifest SHA the run is pinned to")
    tb_run.add_argument("--profile", default="local", help="Model profile (local for offline)")
    tb_run.add_argument("--output", default=None, help="Write the report JSON (and .md) here")

    acc_cmd = subcommands.add_parser(
        "acceptance", help="Full-roster offline acceptance run (TITAN-AGT-007, Vol 2)"
    )
    acc_sub = acc_cmd.add_subparsers(dest="acceptance_command", required=True)
    acc_run = acc_sub.add_parser(
        "run", help="Run the 16-agent Ollama-only acceptance and record it"
    )
    acc_run.add_argument("--profile", default="local-free", help="Model-policy profile (Ollama)")
    acc_run.add_argument("--output", default=None, help="Write the AcceptanceRecord JSON here")
    acc_run.add_argument(
        "--confirm", action="store_true", help="Acknowledge real local-model turns + DB writes"
    )

    sec_cmd = subcommands.add_parser(
        "security", help="Security posture verification (Vol 8 §15, TITAN-SEC-140)"
    )
    sec_sub = sec_cmd.add_subparsers(dest="security_command", required=True)
    sec_check = sec_sub.add_parser(
        "check",
        help="Check the 15 secure-default rows (Settings-backed verified, invariants "
        "asserted); exit non-zero on any failure",
    )
    sec_check.add_argument("--output", default=None, help="Write the scorecard JSON to this path")

    inc_cmd = subcommands.add_parser(
        "incident", help="Incident lockdown + AI playbooks (Vol 8 §13, TITAN-SEC-120)"
    )
    inc_sub = inc_cmd.add_subparsers(dest="incident_command", required=True)
    inc_lock = inc_sub.add_parser("lockdown", help="One-command containment (pause agents ≤60s)")
    inc_lock.add_argument(
        "--reason", default="manual lockdown", help="Why the lockdown was invoked"
    )
    inc_lock.add_argument("--actor", default="cli", help="Who invoked it (audited)")
    inc_lock.add_argument(
        "--confirm", action="store_true", help="Acknowledge this halts ALL agents + revokes tokens"
    )
    inc_fam = inc_sub.add_parser("revoke-family", help="Revoke one refresh-token family")
    inc_fam.add_argument("family_id")
    inc_fam.add_argument("--actor", default="cli")
    inc_kill = inc_sub.add_parser("kill-sandbox", help="Destroy all sandboxes for a task")
    inc_kill.add_argument("task_id")
    inc_kill.add_argument("--actor", default="cli")
    inc_pb = inc_sub.add_parser("playbook", help="Print an AI-incident playbook (P1–P4 or list)")
    inc_pb.add_argument("which", help="P1 | P2 | P3 | P4 | list")

    rt_cmd = subcommands.add_parser(
        "redteam", help="LLM red-team corpus (Vol 8 §14.4, TITAN-SEC-130)"
    )
    rt_sub = rt_cmd.add_subparsers(dest="redteam_command", required=True)
    rt_run = rt_sub.add_parser(
        "run", help="Run the corpus against the real controls; exit non-zero on a regression"
    )
    rt_run.add_argument("--output", default=None, help="Write the scorecard JSON to this path")

    subcommands.add_parser(
        "health", help="Dependency readiness check (Postgres+Redis) for K8s probes (OPS-008)"
    )

    flags_cmd = subcommands.add_parser(
        "flags", help="Operational feature flags / kill switches (Vol 9 §5.3, TITAN-OPS-012)"
    )
    flags_sub = flags_cmd.add_subparsers(dest="flags_command", required=True)
    flags_sub.add_parser("list", help="List every flag and its state")
    flags_get = flags_sub.add_parser("get", help="Print one flag's state")
    flags_get.add_argument("key")
    flags_set = flags_sub.add_parser(
        "set",
        help="Flip a flag on/off (kill switches: agents.pause_all, intake.read_only, "
        "llm.provider_override)",
    )
    flags_set.add_argument("key")
    flags_set.add_argument("state", help="on|off (also true|false)")
    flags_set.add_argument(
        "--value", default=None, help="Optional string payload (e.g. a provider override)"
    )
    flags_set.add_argument("--actor", default="cli:operator", help="Audit actor for the change")
    return parser


def _health_check() -> int:
    """Dependency readiness for K8s exec probes (TITAN-OPS-008): the worker has no HTTP
    server, so its readiness gates on this — Postgres + Redis reachable → exit 0, else 1
    naming the failures. Reuses the same checks the api /readyz endpoint runs."""
    from titan.observability.health import _check_postgres, _check_redis

    async def run() -> int:
        engine = clients.create_engine(get_settings())
        redis = clients.create_redis(get_settings())
        failing: list[str] = []
        try:
            try:
                await _check_postgres(engine)
            except Exception as exc:  # noqa: BLE001 — any failure means unready
                failing.append(f"postgres: {exc}")
            try:
                await _check_redis(redis)
            except Exception as exc:  # noqa: BLE001 — any failure means unready
                failing.append(f"redis: {exc}")
        finally:
            await redis.aclose()
            await engine.dispose()
        if failing:
            for item in failing:
                print(f"UNREADY {item}", file=sys.stderr)
            return 1
        print("ready: postgres, redis")
        return 0

    return asyncio.run(run())


_FLAG_TRUE = {"on", "true", "1", "enabled", "yes"}
_FLAG_FALSE = {"off", "false", "0", "disabled", "no"}


async def _run_flags(run: Callable[[FlagStore, object], Awaitable[int]]) -> int:
    """Wire a real flag store (Postgres + Redis) for the CLI, mirroring the API's
    composition, then dispose it — a `titan` invocation is a short-lived process."""
    engine = clients.create_engine(get_settings())
    redis = clients.create_redis(get_settings())
    try:
        return await run(build_flag_store(engine, redis), engine)
    finally:
        await redis.aclose()
        await engine.dispose()


def _flags_list() -> int:
    async def run(store: FlagStore, _engine: object) -> int:
        for flag in sorted(await store.list_flags(), key=lambda f: f.key):
            suffix = f"  value={flag.value}" if flag.value is not None else ""
            print(f"{'on ' if flag.enabled else 'off'}  {flag.key}{suffix}")
        return 0

    return asyncio.run(_run_flags(run))


def _flags_get(key: str) -> int:
    async def run(store: FlagStore, _engine: object) -> int:
        flag = await store.get(key)
        if flag is None:
            print(f"flag '{key}' not found", file=sys.stderr)
            return 1
        suffix = f"  value={flag.value}" if flag.value is not None else ""
        print(f"{'on ' if flag.enabled else 'off'}  {flag.key}{suffix}")
        return 0

    return asyncio.run(_run_flags(run))


def _flags_set(key: str, state: str, value: str | None, actor: str) -> int:
    low = state.strip().lower()
    if low in _FLAG_TRUE:
        enabled = True
    elif low in _FLAG_FALSE:
        enabled = False
    else:
        print(f"state must be on|off (got '{state}')", file=sys.stderr)
        return 2

    async def run(store: FlagStore, engine: object) -> int:
        changes: dict[str, object] = {"enabled": enabled}
        if value is not None:
            changes["value"] = value
        flag = await store.update(key, changes)
        if flag is None:
            print(f"flag '{key}' not found (seed it via the admin API first)", file=sys.stderr)
            return 1
        # A kill-switch flip is a sensitive operational action — audit it like the
        # incident path does, so the immutable ledger records who toggled what.
        await build_audit_client(engine).record(  # type: ignore[arg-type]
            actor=actor,
            action=AuditAction.INCIDENT_TOGGLE,
            resource=f"flag:{key}",
            outcome="success",
            detail={"enabled": enabled, "value": value},
        )
        suffix = f" value={flag.value}" if flag.value is not None else ""
        print(f"set {key}: enabled={flag.enabled}{suffix}")
        return 0

    return asyncio.run(_run_flags(run))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings().log_level, service="titan-cli")
    if args.command == "memory" and args.memory_command == "rebuild-index":
        return asyncio.run(_rebuild_index())
    if args.command == "benchmark":
        if args.benchmark_command == "list":
            return _benchmark_list()
        if args.benchmark_command == "run":
            try:
                return _benchmark_run(args.brief, args.profile, args.output, args.confirm)
            except (ValueError, KeyError) as exc:
                print(f"benchmark run failed: {exc}", file=sys.stderr)
                return 2
    if args.command == "bench":
        if args.bench_command == "list":
            return _bench_list()
        if args.bench_command == "run":
            try:
                return _bench_run(args.track, args.manifest, args.profile, args.output)
            except (ValueError, KeyError, OSError) as exc:
                print(f"bench run failed: {exc}", file=sys.stderr)
                return 2
    if args.command == "acceptance" and args.acceptance_command == "run":
        return _acceptance_run(args.profile, args.output, args.confirm)
    if args.command == "security" and args.security_command == "check":
        return _security_check(args.output)
    if args.command == "incident":
        if args.incident_command == "lockdown":
            return _incident_lockdown(args.reason, args.actor, args.confirm)
        if args.incident_command == "revoke-family":
            return _incident_revoke_family(args.family_id, args.actor)
        if args.incident_command == "kill-sandbox":
            return _incident_kill_sandbox(args.task_id, args.actor)
        if args.incident_command == "playbook":
            return _incident_playbook(args.which)
    if args.command == "redteam" and args.redteam_command == "run":
        return _redteam_run(args.output)
    if args.command == "health":
        return _health_check()
    if args.command == "flags":
        if args.flags_command == "list":
            return _flags_list()
        if args.flags_command == "get":
            return _flags_get(args.key)
        if args.flags_command == "set":
            return _flags_set(args.key, args.state, args.value, args.actor)
    return 2  # unreachable: argparse enforces the subcommand set


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    sys.exit(main())
