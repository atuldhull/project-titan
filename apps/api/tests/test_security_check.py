"""Phase 75 (Vol 8 §15): the TITAN-SEC-140 secure-defaults checker.

DoD: `titan security check` is green on the default install and exits non-zero on a
seeded misconfiguration. Also re-broken: every §15 row binds to a real probe, and a
row whose check key has no probe FAILS (never a silent pass). Pure — no DB/network.
"""

from __future__ import annotations

from typing import Any

from titan.cli import _COMPLIANCE_MAP, _build_security_context, _hosts_are_internal
from titan.security import (
    CHECKS,
    SecureDefaultRow,
    SecurityContext,
    evaluate,
    load_secure_default_rows,
)
from titan.settings import Settings

pytestmark: list[Any] = []

_ROWS = load_secure_default_rows(_COMPLIANCE_MAP)


def _settings(**over: Any) -> Settings:
    return Settings(otel_exporter_otlp_endpoint="", _env_file=None, **over)


def test_checklist_has_15_rows_all_bound_to_a_probe() -> None:
    assert len(_ROWS) == 15
    assert {r.id for r in _ROWS} == set(range(1, 16))
    for row in _ROWS:
        assert row.check in CHECKS, f"row {row.id} check '{row.check}' has no probe"


# The secure baseline: explicit init kwargs WIN over any OS env leakage (a dev shell
# may export TITAN_RATE_LIMIT_ENABLED=false etc.), so this is the true default posture.
_SECURE = dict(
    sandbox_egress_network="",
    auto_deploy_enabled=False,
    auto_merge_enabled=False,
    require_tls=True,
    hsts_enabled=True,
    rate_limit_enabled=True,
    vault_age_key="AGE-SECRET-KEY-1EXAMPLE",  # a configured writable vault (row 7)
    debug=False,  # detail suppression on (row 14)
)


def test_secure_default_posture_passes_all_15() -> None:
    card = evaluate(_build_security_context(_settings(**_SECURE)), _ROWS)
    assert card.passed, [f"{r.row_id}:{r.detail}" for r in card.failures]
    assert len(card.results) == 15
    # The all-secure context (built-in defaults) is likewise green.
    assert evaluate(SecurityContext(), _ROWS).passed


def test_newly_live_rows_can_actually_fail() -> None:
    # review #4/#7: vault, budget, and error-detail rows are now resolved from live
    # Settings and MUST be able to FAIL, not decoratively pass.
    no_vault = evaluate(
        _build_security_context(_settings(**{**_SECURE, "vault_age_key": ""})), _ROWS
    )
    assert any(r.row_id == 7 and r.status.value == "fail" for r in no_vault.results)
    debug_on = evaluate(_build_security_context(_settings(**{**_SECURE, "debug": True})), _ROWS)
    assert any(r.row_id == 14 and r.status.value == "fail" for r in debug_on.results)


def test_seeded_egress_misconfig_is_detected() -> None:
    # An operator who opens sandbox egress relaxes secure-default #1.
    card = evaluate(_build_security_context(_settings(sandbox_egress_network="bridge")), _ROWS)
    assert not card.passed
    assert any(r.row_id == 1 and r.status.value == "fail" for r in card.results)


def test_seeded_auto_deploy_misconfig_is_detected() -> None:
    card = evaluate(_build_security_context(_settings(auto_deploy_enabled=True)), _ROWS)
    assert not card.passed
    assert any(r.row_id == 2 and r.status.value == "fail" for r in card.results)


def test_seeded_hsts_off_is_detected() -> None:
    card = evaluate(_build_security_context(_settings(hsts_enabled=False)), _ROWS)
    assert any(r.row_id == 5 and r.status.value == "fail" for r in card.results)


def test_internal_binding_excludes_client_targets_and_fails_on_a_public_bind() -> None:
    # The egress proxy/gateway + the Docker daemon URL bind/target 0.0.0.0 or a
    # client host BY DESIGN — all excluded (review #8), so they don't false-positive.
    assert _hosts_are_internal(
        _settings(
            egress_forward_host="0.0.0.0",
            egress_gateway_host="0.0.0.0",
            sandbox_docker_host="0.0.0.0",
        )
    )
    # A genuine public bind (context signal false) fails the row.
    assert not evaluate(SecurityContext(internal_binding=False), _ROWS).passed


def test_unbound_check_key_fails_not_silently_passes() -> None:
    rogue = [SecureDefaultRow(id=1, default="x", ref=[], check="does_not_exist")]
    card = evaluate(SecurityContext(), rogue)
    assert not card.passed
    assert "no probe bound" in card.results[0].detail
