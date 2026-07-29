"""The 15 secure-default checks (TITAN-SEC-140, Vol 8 §15).

Each check is a pure function of :class:`SecurityContext` returning
``(passed, detail)``. :func:`evaluate` binds the §15 rows (loaded from
compliance-map.yaml) to these probes by the row's ``check`` key and produces a
:class:`Scorecard`. Every row MUST bind to a probe — an unbound row is an error,
not a silent pass, so the checklist cannot drift out from under the checker.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from titan.security.model import (
    CheckResult,
    CheckStatus,
    Scorecard,
    SecureDefaultRow,
    SecurityContext,
)

Probe = Callable[[SecurityContext], tuple[bool, str]]


def _egress(c: SecurityContext) -> tuple[bool, str]:
    ok = c.sandbox_egress_network == ""
    return ok, (
        "sandbox network_mode=none (deny-all)"
        if ok
        else f"sandbox_egress_network='{c.sandbox_egress_network}' — egress is NOT deny-all"
    )


def _deploy_merge(c: SecurityContext) -> tuple[bool, str]:
    ok = not c.auto_deploy_enabled and not c.auto_merge_enabled
    return ok, (
        "auto-deploy and auto-merge OFF (human approval gates G6–G12 enforce it)"
        if ok
        else f"auto_deploy={c.auto_deploy_enabled}, auto_merge={c.auto_merge_enabled} (must be off)"
    )


def _internal_binding(c: SecurityContext) -> tuple[bool, str]:
    return c.internal_binding, (
        "services bind loopback/internal; only UI/API exposed"
        if c.internal_binding
        else "a service is configured to bind a public interface"
    )


def _first_admin_mfa(c: SecurityContext) -> tuple[bool, str]:
    return c.mfa_available, (
        "MFA (TOTP) available and first-admin change-on-first-login enforced"
        if c.mfa_available
        else "MFA machinery is not available"
    )


def _tls_hsts(c: SecurityContext) -> tuple[bool, str]:
    ok = c.require_tls and c.hsts_enabled
    return ok, (
        "TLS required on non-loopback + HSTS on public endpoints"
        if ok
        else f"require_tls={c.require_tls}, hsts_enabled={c.hsts_enabled}"
    )


def _jwt(c: SecurityContext) -> tuple[bool, str]:
    ok = 0 < c.access_ttl_seconds <= 900 and c.refresh_rotation
    return ok, (
        f"access token TTL {c.access_ttl_seconds}s (<=900) + refresh rotation on"
        if ok
        else f"access_ttl_seconds={c.access_ttl_seconds}, refresh_rotation={c.refresh_rotation}"
    )


def _vault(c: SecurityContext) -> tuple[bool, str]:
    ok = c.vault_initialized and c.secret_file_exclusion
    return ok, (
        "SOPS+age vault initialized + secret-file patterns excluded from context/RAG"
        if ok
        else f"vault_initialized={c.vault_initialized}, secret_exclusion={c.secret_file_exclusion}"
    )


def _redaction(c: SecurityContext) -> tuple[bool, str]:
    return c.redaction_wired, (
        "redaction pipeline wired on gateway + logs + sandbox sinks"
        if c.redaction_wired
        else "redaction is not wired into all three sinks"
    )


def _rate_budget(c: SecurityContext) -> tuple[bool, str]:
    ok = c.rate_limit_enabled and c.budget_caps_configured
    return ok, (
        "rate limits enabled + LLM budget caps configured"
        if ok
        else f"rate_limit_enabled={c.rate_limit_enabled}, budget_caps={c.budget_caps_configured}"
    )


def _audit(c: SecurityContext) -> tuple[bool, str]:
    return c.audit_chain_active, (
        "audit_events INSERT-only (mutation trigger) + hash chain verifies"
        if c.audit_chain_active
        else "audit chain is not active (trigger missing or verify failed)"
    )


def _containers(c: SecurityContext) -> tuple[bool, str]:
    return c.sandbox_hardened, (
        "sandbox containers non-root, read-only rootfs, caps dropped, limits set"
        if c.sandbox_hardened
        else "sandbox container hardening is relaxed"
    )


def _gen_gates(c: SecurityContext) -> tuple[bool, str]:
    return c.generated_project_gates, (
        "generated projects ship the CI security gates by default"
        if c.generated_project_gates
        else "generated-project security gates are off by default"
    )


def _memory_ns(c: SecurityContext) -> tuple[bool, str]:
    return c.memory_namespaced, (
        "per-project memory namespaces; cross-project retrieval off until promotion"
        if c.memory_namespaced
        else "memory namespace isolation is off"
    )


def _errors(c: SecurityContext) -> tuple[bool, str]:
    return c.error_detail_suppressed, (
        "generic problem+json errors + correlation IDs (detail suppression on)"
        if c.error_detail_suppressed
        else "error-detail suppression is off (internal detail may leak)"
    )


def _observability(c: SecurityContext) -> tuple[bool, str]:
    return c.observability_authed, (
        "Prometheus/Grafana/Loki endpoints auth-protected, not public"
        if c.observability_authed
        else "an observability endpoint is publicly exposed"
    )


CHECKS: dict[str, Probe] = {
    "sandbox_egress_deny_all": _egress,
    "deploy_merge_require_approval": _deploy_merge,
    "internal_binding": _internal_binding,
    "first_admin_mfa": _first_admin_mfa,
    "tls_and_hsts": _tls_hsts,
    "jwt_short_lived_rotating": _jwt,
    "vault_and_secret_exclusion": _vault,
    "redaction_on_sinks": _redaction,
    "rate_limits_and_budgets": _rate_budget,
    "audit_chain_active": _audit,
    "sandbox_container_hardening": _containers,
    "generated_project_gates": _gen_gates,
    "memory_namespace_isolation": _memory_ns,
    "generic_errors_correlation": _errors,
    "observability_auth_protected": _observability,
}

# Checks whose signal is a CONSTRUCTION-TIME invariant (asserted at its Phase-N
# secure default) rather than a live, operator-flippable setting the composition root
# resolves dynamically. Their PASS is honestly labelled so the scorecard never
# overstates what was actually inspected (SEC-140 review #4/#7/#8). The remaining
# rows (egress, auto-deploy/merge, TLS/HSTS, vault, rate/budget, sandbox policy,
# error-detail, internal-binding) ARE resolved from live Settings/policy.
ASSERTED_CHECKS = frozenset(
    {
        "first_admin_mfa",
        "redaction_on_sinks",
        "audit_chain_active",
        "generated_project_gates",
        "memory_namespace_isolation",
        "observability_auth_protected",
    }
)


def load_secure_default_rows(map_path: Path) -> list[SecureDefaultRow]:
    doc = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    rows = doc.get("secure_defaults") or []
    return [
        SecureDefaultRow(
            id=int(r["id"]),
            default=str(r["default"]),
            ref=list(r.get("ref") or []),
            check=str(r["check"]),
        )
        for r in rows
    ]


def evaluate(ctx: SecurityContext, rows: list[SecureDefaultRow]) -> Scorecard:
    """Score every §15 row against ``ctx``. A row whose ``check`` key has no probe
    is a FAIL (misconfiguration of the checklist itself), never a silent skip."""
    results: list[CheckResult] = []
    for row in sorted(rows, key=lambda r: r.id):
        probe = CHECKS.get(row.check)
        if probe is None:
            results.append(
                CheckResult(
                    row.id,
                    row.default,
                    row.ref,
                    CheckStatus.FAIL,
                    f"no probe bound to check '{row.check}' (checklist/checker drift)",
                )
            )
            continue
        passed, detail = probe(ctx)
        if passed and row.check in ASSERTED_CHECKS:
            detail = f"{detail} [asserted construction-time invariant, not live-probed]"
        results.append(
            CheckResult(
                row.id,
                row.default,
                row.ref,
                CheckStatus.PASS if passed else CheckStatus.FAIL,
                detail,
            )
        )
    return Scorecard(results)
