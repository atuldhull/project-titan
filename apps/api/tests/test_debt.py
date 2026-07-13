"""The technical-debt ledger (Phase 49; Vol 6 §5, TITAN-WFL-021). Schema-marked —
needs the test DB. Proves the ledger domain and the WFL-021 invariant that every
APPROVED_WITH_COMMENTS verdict (and accepted medium security finding) opens a ticket."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.debt import service as debt
from titan.handoffs import service as handoffs

pytestmark = pytest.mark.schema


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "u": uid},
        )
    return pid


def test_open_ticket_is_idempotent_and_ledger_totals_open(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))

    async def scenario() -> tuple[int, int, str]:
        async with migrated_engine.begin() as conn:
            first = await debt.open_ticket(
                conn, pid, source="code_review", title="t", fingerprint="fp-1", detail="d1"
            )
            # Same fingerprint → same ticket (no duplicate).
            again = await debt.open_ticket(
                conn, pid, source="code_review", title="t2", fingerprint="fp-1", detail="d2"
            )
            await debt.open_ticket(conn, pid, source="security", title="s", fingerprint="fp-2")
        async with migrated_engine.begin() as conn:
            ledger = await debt.ledger(conn, pid)
            await debt.resolve_ticket(conn, first.id)
        async with migrated_engine.connect() as conn:
            after = await debt.ledger(conn, pid)
        return ledger.open_total, after.open_total, str(first.id == again.id)

    open_before, open_after, same = run_async(scenario)
    assert same == "True"  # idempotent on fingerprint
    assert open_before == 2  # two distinct fingerprints
    assert open_after == 1  # one resolved → paid


def test_acwc_release_review_opens_a_debt_ticket_per_comment(migrated_engine: Any) -> None:
    """TITAN-WFL-021: every APPROVED_WITH_COMMENTS verdict has a debt ticket."""
    pid = run_async(lambda: _make_project(migrated_engine))
    body = {
        "artifact_type": "ReleaseReview",
        "verdict": "APPROVED_WITH_COMMENTS",
        "comments": ["duplication in billing util", "public API naming drift"],
    }

    async def scenario() -> list[Any]:
        async with migrated_engine.begin() as conn:
            await handoffs._open_debt_after_pass(conn, pid, "code_review", body)
        async with migrated_engine.connect() as conn:
            return (await debt.ledger(conn, pid)).tickets

    tickets = run_async(scenario)
    assert len(tickets) == 2
    assert all(t.source == "code_review" for t in tickets)
    assert {t.detail for t in tickets} == {"duplication in billing util", "public API naming drift"}
    assert all(t.paydown_proposal for t in tickets)  # a paydown is always proposed


def test_acwc_with_no_itemized_comments_still_opens_one_ticket(migrated_engine: Any) -> None:
    """WFL-021 is per-VERDICT: an APPROVED_WITH_COMMENTS with an empty comments list
    still opens a debt ticket, so 'every ACWC verdict has a ticket' holds."""
    pid = run_async(lambda: _make_project(migrated_engine))
    body = {"artifact_type": "ReleaseReview", "verdict": "APPROVED_WITH_COMMENTS", "comments": []}

    async def scenario() -> list[Any]:
        async with migrated_engine.begin() as conn:
            await handoffs._open_debt_after_pass(conn, pid, "code_review", body)
        async with migrated_engine.connect() as conn:
            return (await debt.ledger(conn, pid)).tickets

    tickets = run_async(scenario)
    assert len(tickets) == 1 and tickets[0].source == "code_review"


def test_plain_approved_review_opens_no_debt(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    body = {"artifact_type": "ReleaseReview", "verdict": "APPROVED", "comments": []}

    async def scenario() -> int:
        async with migrated_engine.begin() as conn:
            await handoffs._open_debt_after_pass(conn, pid, "code_review", body)
        async with migrated_engine.connect() as conn:
            return (await debt.ledger(conn, pid)).open_total

    assert run_async(scenario) == 0


def test_accepted_medium_security_finding_opens_debt(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    body = {
        "artifact_type": "SecurityClearance",
        "sast": [{"id": "S1", "severity": "medium", "title": "weak hash", "evidence": "e"}],
        "dependency_audit": [{"package": "libx", "cve_id": "CVE-1", "severity": "medium"}],
    }

    async def scenario() -> list[Any]:
        async with migrated_engine.begin() as conn:
            await handoffs._open_debt_after_pass(conn, pid, "security", body)
        async with migrated_engine.connect() as conn:
            return (await debt.ledger(conn, pid)).tickets

    tickets = run_async(scenario)
    assert len(tickets) == 2 and all(t.source == "security" for t in tickets)


def test_debt_model_maps_the_table() -> None:
    from titan.debt.models import DebtTicket

    assert DebtTicket.__tablename__ == "debt_tickets"
    cols = set(DebtTicket.__table__.columns.keys())
    assert {"source", "severity", "fingerprint", "status", "paydown_task_id"} <= cols
    assert {"created_at", "updated_at", "version"} <= cols


# --- REST endpoints (dashboard widgets) --------------------------------------


def _project_via_api(api: dict[str, Any]) -> uuid.UUID:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(str(resp.json()["id"]))


def test_debt_ledger_rest_endpoint(api: dict[str, Any]) -> None:
    pid = _project_via_api(api)

    async def seed() -> None:
        async with api["engine"].begin() as conn:
            await debt.open_ticket(
                conn, pid, source="code_review", title="dup util", fingerprint="fp-x"
            )

    run_async(seed)
    resp = api["client"].get(f"/api/v1/projects/{pid}/debt", headers=api["token_for"]("owner"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["open_total"] == 1
    assert body["tickets"][0]["source"] == "code_review"


def test_traceability_rest_endpoint(api: dict[str, Any]) -> None:
    pid = _project_via_api(api)
    resp = api["client"].get(
        f"/api/v1/projects/{pid}/traceability", headers=api["token_for"]("owner")
    )
    assert resp.status_code == 200, resp.text
    # A fresh project has no artifacts → an empty table with no gaps.
    assert resp.json() == {"rows": [], "gaps": []}
