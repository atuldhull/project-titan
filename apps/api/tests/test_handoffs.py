"""Phase 42 — the handoff executor + gate ledger over the real schema
(Vol 6 §2.2, TITAN-WFL-003/004/005).

Covers the DoD: an artifact missing a required field is rejected with a
field-level error report attached to the failure event; a stage forced to fail 4
times produces exactly one escalation, lands the project in NEEDS_HUMAN, and does
zero further work; the gate ledger exports every pass/fail with reasons.
"""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from tests.test_handoff_schemas import EXAMPLES
from titan.handoffs import gates
from titan.handoffs import service as handoffs

pytestmark = pytest.mark.schema

VALID_BRIEF = EXAMPLES["ProjectBrief"]


@pytest.fixture(autouse=True)
def _reset_gates() -> Iterator[None]:
    """The gate registry is module-global; isolate every test."""
    gates.clear_gates()
    yield
    gates.clear_gates()


def _project(api: dict[str, Any], owner: str = "owner") -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"](owner),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _submit(
    api: dict[str, Any], pid: str, stage: str, body: dict[str, Any], token: str = "owner"
) -> Any:
    return api["client"].post(
        f"/api/v1/projects/{pid}/handoffs/{stage}", json=body, headers=api["token_for"](token)
    )


def _pipeline(api: dict[str, Any], pid: str) -> dict[str, Any]:
    r = api["client"].get(f"/api/v1/projects/{pid}/pipeline", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    return r.json()["pipeline"]


def _ledger(api: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    r = api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/gate-ledger", headers=api["token_for"]("owner")
    )
    assert r.status_code == 200, r.text
    return r.json()["evaluations"]


# --- happy path + schema gate ----------------------------------------------


def test_submit_valid_artifact_advances_the_pipeline(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _submit(api, pid, "ceo_vision", VALID_BRIEF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "advanced" and body["next_stage"] == "pm_backlog"
    assert body["artifact_version"] == 1
    assert _pipeline(api, pid)["current_stage"] == "pm_backlog"
    # The artifact is stored and retrievable; the gate ledger has one pass row.
    art = api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/ceo_vision/artifact", headers=api["token_for"]("owner")
    )
    assert art.status_code == 200 and art.json()["artifact_type"] == "ProjectBrief"
    ledger = _ledger(api, pid)
    assert len(ledger) == 1 and ledger[0]["verdict"] == "pass"
    assert ledger[0]["evaluator_agent"] and ledger[0]["prompt_version"]  # evaluator identity


def test_schema_rejection_reports_field_errors(api: dict[str, Any]) -> None:
    """TITAN-WFL-003 DoD: a missing required field is rejected with a field-level
    error report attached to the failure event, and counts as a gate failure."""
    pid = _project(api)
    r = _submit(api, pid, "ceo_vision", {"target_users": ["u"]})  # missing vision, features, …
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "schema_rejected"
    assert any("vision" in e for e in body["field_errors"])
    # Counts as a gate failure → retry, still at ceo_vision, iteration bumped.
    assert body["outcome"] == "schema_rejected" and body["attempt"] == 1
    assert _pipeline(api, pid)["current_stage"] == "ceo_vision"
    # The failure event (gate ledger row) carries the field-level reasons.
    ledger = _ledger(api, pid)
    assert len(ledger) == 1 and ledger[0]["verdict"] == "fail"
    assert any("vision" in str(x) for x in ledger[0]["reasons"])
    # No artifact was stored for the rejected submission.
    art = api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/ceo_vision/artifact", headers=api["token_for"]("owner")
    )
    assert art.status_code == 404


# --- retry budget + escalation (TITAN-WFL-005) ------------------------------


def test_gate_failure_retries_within_budget(api: dict[str, Any]) -> None:
    pid = _project(api)
    gates.register_gate("ceo_vision", gates.ConfigurableGate(passes=False, reasons=["G2: gap"]))
    for expected_attempt in (1, 2, 3):
        r = _submit(api, pid, "ceo_vision", VALID_BRIEF)
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "gate_failed_retry"
        assert r.json()["attempt"] == expected_attempt
        assert r.json()["gate_reasons"] == ["G2: gap"]
    assert _pipeline(api, pid)["current_stage"] == "ceo_vision"
    assert _pipeline(api, pid)["status"] == "running"


def test_four_failures_escalate_once_and_pause(api: dict[str, Any]) -> None:
    """DoD: a stage forced to fail 4 times produces exactly one escalation, the
    project lands in NEEDS_HUMAN, and zero further work occurs for that stage."""
    pid = _project(api)
    gates.register_gate("ceo_vision", gates.ConfigurableGate(passes=False, reasons=["G2: gap"]))
    outcomes = [_submit(api, pid, "ceo_vision", VALID_BRIEF).json()["outcome"] for _ in range(4)]
    assert outcomes == ["gate_failed_retry", "gate_failed_retry", "gate_failed_retry", "escalated"]
    assert _pipeline(api, pid)["status"] == "needs_human"

    # A 5th submission is refused (pipeline not running) — zero further evaluation.
    fifth = _submit(api, pid, "ceo_vision", VALID_BRIEF)
    assert fifth.status_code == 409

    ledger = _ledger(api, pid)
    assert len(ledger) == 4 and all(e["verdict"] == "fail" for e in ledger)

    async def _counts() -> tuple[int, int]:
        async with api["engine"].connect() as conn:
            needs_human = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pipeline_stage_events "
                        "WHERE project_id = :p AND event = 'needs_human'"
                    ),
                    {"p": uuid.UUID(pid)},
                )
            ).scalar_one()
            notes = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM notifications "
                        "WHERE category = 'pipeline' AND link = :link"
                    ),
                    {"link": f"/projects/{pid}/handoffs/gate-ledger"},
                )
            ).scalar_one()
        return int(needs_human), int(notes)

    needs_human, notes = run_async(_counts)
    assert needs_human == 1  # exactly one escalation logged
    assert notes == 1  # exactly one structured escalation notification to the owner


def test_concurrent_failures_at_budget_escalate_exactly_once(api: dict[str, Any]) -> None:
    """Regression (adversarial review): two concurrent failing submissions at the
    retry-budget boundary must NOT double-escalate. The FOR-UPDATE lock on the
    pipeline row serializes them per project — exactly one escalation, one owner
    notification (TITAN-WFL-005)."""
    pid = _project(api)
    gates.register_gate("ceo_vision", gates.ConfigurableGate(passes=False, reasons=["G2: gap"]))
    engine = api["engine"]
    pid_u = uuid.UUID(pid)

    async def scenario() -> list[Any]:
        # Three sequential failures — the budget (3) is not yet exhausted.
        for _ in range(3):
            await handoffs.submit_artifact(
                engine, pid_u, "ceo_vision", VALID_BRIEF, submitted_by="ceo"
            )
        # Two concurrent 4th submissions race the escalation boundary.
        return await asyncio.gather(
            handoffs.submit_artifact(engine, pid_u, "ceo_vision", VALID_BRIEF, submitted_by="ceo"),
            handoffs.submit_artifact(engine, pid_u, "ceo_vision", VALID_BRIEF, submitted_by="ceo"),
            return_exceptions=True,
        )

    results = run_async(scenario)
    escalated = [
        r for r in results if not isinstance(r, BaseException) and r.outcome == "escalated"
    ]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(escalated) == 1  # exactly one escalated
    assert len(refused) == 1  # the other was refused (pipeline already NEEDS_HUMAN)

    async def _counts() -> tuple[int, int]:
        async with engine.connect() as conn:
            nh = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pipeline_stage_events "
                        "WHERE project_id = :p AND event = 'needs_human'"
                    ),
                    {"p": pid_u},
                )
            ).scalar_one()
            # Scope to THIS project (the notification links to its gate ledger) —
            # the session DB is shared across tests, so a global count would also
            # see other escalation tests' notifications.
            notes = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM notifications "
                        "WHERE category = 'pipeline' AND link = :link"
                    ),
                    {"link": f"/projects/{pid}/handoffs/gate-ledger"},
                )
            ).scalar_one()
        return int(nh), int(notes)

    needs_human, notes = run_async(_counts)
    assert needs_human == 1  # exactly one escalation despite the concurrent race
    assert notes == 1  # exactly one owner notification


# --- guards -----------------------------------------------------------------


def test_out_of_order_stage_rejected(api: dict[str, Any]) -> None:
    """A handoff cannot skip ahead: submitting a later stage's artifact while the
    pipeline is at ceo_vision is a 409 (handoffs ARE the pipeline transitions)."""
    pid = _project(api)
    r = _submit(api, pid, "pm_backlog", EXAMPLES["ProductBacklog"])
    assert r.status_code == 409


def test_unknown_or_nonproducing_stage_422(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _submit(api, pid, "handover", {}).status_code == 422  # terminal, no artifact
    assert _submit(api, pid, "bogus", {}).status_code == 422


def test_budget_hold_pauses_the_handoff(api: dict[str, Any]) -> None:
    pid = _project(api)

    async def _hold() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
                    "VALUES (:id, 'project', :p, 'reached', 100)"
                ),
                {"id": uuid.uuid4(), "p": uuid.UUID(pid)},
            )

    run_async(_hold)
    r = _submit(api, pid, "ceo_vision", VALID_BRIEF)
    assert r.status_code == 409 and "budget" in r.json()["detail"].lower()
    # No gate was evaluated (no LLM would be spent), so the ledger is empty.
    assert _ledger(api, pid) == []


def test_submit_requires_advance_permission(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _submit(api, pid, "ceo_vision", VALID_BRIEF, token="viewer").status_code == 403


def test_gate_ledger_is_append_only(api: dict[str, Any]) -> None:
    pid = _project(api)
    _submit(api, pid, "ceo_vision", VALID_BRIEF)  # one pass row

    async def _mutate() -> str:
        try:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text("UPDATE gate_evaluations SET verdict = 'tampered' WHERE project_id = :p"),
                    {"p": uuid.UUID(pid)},
                )
            return "no-error"
        except Exception as exc:  # noqa: BLE001 — asserting the trigger fires
            return type(exc).__name__

    assert run_async(_mutate) != "no-error"
