"""Data model for the secure-defaults checker (TITAN-SEC-140)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class CheckStatus(enum.StrEnum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class SecureDefaultRow:
    """One §15 checklist row, loaded from compliance-map.yaml."""

    id: int
    default: str
    ref: list[str]
    check: str  # the stable key binding this row to a probe in CHECKS


@dataclass(frozen=True)
class SecurityContext:
    """Real signals gathered from a live install by the composition root. Every
    field defaults to its SECURE value so a test constructs a green context and
    flips exactly one field to prove a check bites."""

    # Settings-backed (flippable) posture.
    sandbox_egress_network: str = ""  # "" == deny-all (network_mode=none)
    auto_deploy_enabled: bool = False
    auto_merge_enabled: bool = False
    require_tls: bool = True
    hsts_enabled: bool = True
    rate_limit_enabled: bool = True
    access_ttl_seconds: int = 900  # JWT ceiling (identity.tokens.ACCESS_TTL_SECONDS)

    # Code-invariant signals the composition root resolves from real defaults.
    internal_binding: bool = True
    mfa_available: bool = True
    refresh_rotation: bool = True
    vault_initialized: bool = True
    secret_file_exclusion: bool = True
    redaction_wired: bool = True
    budget_caps_configured: bool = True
    audit_chain_active: bool = True
    sandbox_hardened: bool = True
    generated_project_gates: bool = True
    memory_namespaced: bool = True
    error_detail_suppressed: bool = True
    observability_authed: bool = True


@dataclass(frozen=True)
class CheckResult:
    row_id: int
    name: str
    ref: list[str]
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class Scorecard:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status is CheckStatus.PASS for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.FAIL]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "total": len(self.results),
            "failed": len(self.failures),
            "rows": [
                {
                    "id": r.row_id,
                    "default": r.name,
                    "ref": r.ref,
                    "status": r.status.value,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }
