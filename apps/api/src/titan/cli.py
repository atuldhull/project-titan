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
from pathlib import Path

from titan import clients
from titan.benchmark.egress_guard import EgressGuard, guarded_client
from titan.benchmark.kpi import KpiReport
from titan.benchmark.offline_acceptance import AcceptanceRecord, run_offline_acceptance
from titan.benchmark.runner import BenchmarkRunner
from titan.benchmark.suite import load_suite
from titan.llm.service import build_gateway
from titan.memory import service as memory
from titan.observability.logging import configure_logging
from titan.runtime.service import build_runtime
from titan.settings import get_settings


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
    return parser


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
    if args.command == "acceptance" and args.acceptance_command == "run":
        return _acceptance_run(args.profile, args.output, args.confirm)
    return 2  # unreachable: argparse enforces the subcommand set


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    sys.exit(main())
