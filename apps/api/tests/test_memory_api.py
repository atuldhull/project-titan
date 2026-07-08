"""The Memory Service write/read path (Vol 5 §4/§5.1/§14, TITAN-BRN-001/005),
Phase 33 — needs the disposable test DB.

Covers the DoD: an event missing kind/scope is 422 and the DB CHECKs reject
invalid enums directly; a mid-batch failure rolls back to zero rows; N reads +
N writes produce exactly 2N ledger rows. Plus per-endpoint happy + validation +
auth failure."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import run_async

pytestmark = pytest.mark.schema


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _event(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "semantic",
        "scope": "org",
        "content_type": "markdown",
        "title": "Qdrant payload filtering",
        "body_md": "Qdrant supports keyword match and range conditions on payload.",
        "importance": 0.7,
    }
    base.update(over)
    return base


def _write(
    api: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    source_agent: str = "memory_agent",
    token: str = "operator",
    task_id: str | None = None,
) -> Any:
    body: dict[str, Any] = {"source_agent": source_agent, "events": events}
    if task_id is not None:
        body["task_id"] = task_id
    return api["client"].post("/api/v1/memory/events", json=body, headers=api["token_for"](token))


# --- POST /memory/events: happy path ---------------------------------------


def test_write_events_persists_record_chunks_and_outbox(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _write(
        api,
        [
            _event(),
            _event(
                kind="episodic",
                scope=f"project:{pid}",
                title="QA rejected PR #41",
                body_md="2026-07-02: flaky test caused by a shared fixture.",
            ),
        ],
    )
    assert r.status_code == 201, r.text
    records = r.json()["records"]
    assert len(records) == 2 and all(rec["chunk_count"] >= 1 for rec in records)

    rid0 = records[0]["record_id"]

    async def check() -> None:
        async with api["engine"].connect() as conn:
            rec = await conn.execute(
                text(
                    "SELECT kind, scope, project_id, source_agent, importance FROM "
                    "brain.memory_records WHERE id = :id"
                ),
                {"id": rid0},
            )
            row = rec.one()
            assert row.kind == "semantic" and row.scope == "org"
            assert row.project_id is None and row.source_agent == "memory_agent"
            # project-scoped record derives project_id from scope
            proj_rec = await conn.execute(
                text("SELECT project_id FROM brain.memory_records WHERE id = :id"),
                {"id": records[1]["record_id"]},
            )
            assert str(proj_rec.scalar_one()) == pid
            # one ingest_outbox 'upsert' row per chunk
            chunks = await conn.execute(
                text("SELECT count(*) FROM brain.memory_chunks WHERE record_id = :id"),
                {"id": rid0},
            )
            outbox = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.ingest_outbox o JOIN brain.memory_chunks c "
                    "ON c.id = o.chunk_id WHERE c.record_id = :id AND o.op = 'upsert'"
                ),
                {"id": rid0},
            )
            assert int(chunks.scalar_one()) == int(outbox.scalar_one()) >= 1

    run_async(check)


def test_get_record_returns_record_chunks_and_ledger(api: dict[str, Any]) -> None:
    _project(api)
    written = _write(api, [_event()]).json()["records"]
    rid = written[0]["record_id"]

    got = api["client"].get(f"/api/v1/memory/records/{rid}", headers=api["token_for"]("viewer"))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["record"]["id"] == rid
    assert body["record"]["access_count"] == 1  # the read bumped access metadata
    assert len(body["chunks"]) >= 1 and body["chunks"][0]["seq"] == 0
    # the trail holds the write (from the POST) + this read
    actions = sorted(entry["action"] for entry in body["ledger"])
    assert actions == ["read", "write"]


def test_get_unknown_record_is_404(api: dict[str, Any]) -> None:
    r = api["client"].get(
        f"/api/v1/memory/records/{uuid.uuid4()}", headers=api["token_for"]("viewer")
    )
    assert r.status_code == 404


# --- validation (TITAN-BRN-001 acceptance) ---------------------------------


def test_missing_kind_or_scope_is_422(api: dict[str, Any]) -> None:
    no_kind = _event()
    no_kind.pop("kind")
    assert _write(api, [no_kind]).status_code == 422
    no_scope = _event()
    no_scope.pop("scope")
    assert _write(api, [no_scope]).status_code == 422


def test_invalid_enum_and_scope_values_are_422(api: dict[str, Any]) -> None:
    assert _write(api, [_event(kind="rumor")]).status_code == 422
    assert _write(api, [_event(scope="everywhere")]).status_code == 422
    assert _write(api, [_event(scope="project:not-a-uuid")]).status_code == 422
    assert _write(api, [_event(importance=1.5)]).status_code == 422
    assert _write(api, [_event(content_type="binary")]).status_code == 422
    assert _write(api, []).status_code == 422  # empty batch


def test_unknown_project_scope_is_422(api: dict[str, Any]) -> None:
    r = _write(api, [_event(scope=f"project:{uuid.uuid4()}")])
    assert r.status_code == 422  # FK to a non-existent project


def test_oversized_body_is_422_not_500(api: dict[str, Any]) -> None:
    """A body large enough to overflow the generated tsvector is rejected at the
    boundary (422), never a 500 mid-write."""
    from titan.memory.service import MAX_BODY_MD_CHARS

    r = _write(api, [_event(body_md="x " * (MAX_BODY_MD_CHARS))])  # well over the cap
    assert r.status_code == 422


def test_write_requires_memory_write_permission(api: dict[str, Any]) -> None:
    # viewer has only *:read — no memory:write.
    assert _write(api, [_event()], token="viewer").status_code == 403


def test_write_unauthenticated_is_401(api: dict[str, Any]) -> None:
    r = api["client"].post(
        "/api/v1/memory/events",
        json={"source_agent": "memory_agent", "events": [_event()]},
    )
    assert r.status_code == 401


# --- DB CHECK constraints reject invalid enums directly (DoD) ---------------


def test_db_checks_reject_invalid_enum_values(api: dict[str, Any], migrated_engine: Any) -> None:
    pid = _project(api)

    async def scenario() -> None:
        async def _insert_record_kind(kind: str) -> None:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO brain.memory_records (kind, scope, source_agent, title, "
                        "body_md) VALUES (:k, 'org', 'a', 't', 'b')"
                    ),
                    {"k": kind},
                )

        with pytest.raises(IntegrityError):
            await _insert_record_kind("rumor")  # ck_memory_records_kind

        with pytest.raises(IntegrityError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO brain.memory_records (kind, scope, source_agent, title, "
                        "body_md, status) VALUES ('semantic', 'org', 'a', 't', 'b', 'zombie')"
                    )
                )

        with pytest.raises(IntegrityError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO brain.memory_ledger (actor_agent, action, record_ids) "
                        "VALUES ('a', 'teleport', ARRAY[]::uuid[])"
                    )
                )

        # A valid record then a bad ingest_outbox op.
        async with migrated_engine.begin() as conn:
            rid = (
                await conn.execute(
                    text(
                        "INSERT INTO brain.memory_records (kind, scope, source_agent, title, "
                        "body_md) VALUES ('semantic', 'org', 'a', 't', 'b') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await conn.execute(
                    text(
                        "INSERT INTO brain.memory_chunks (record_id, seq, text, token_count) "
                        "VALUES (:rid, 0, 'x', 1) RETURNING id"
                    ),
                    {"rid": rid},
                )
            ).scalar_one()
        with pytest.raises(IntegrityError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO brain.ingest_outbox (chunk_id, op) VALUES (:cid, 'frobnicate')"
                    ),
                    {"cid": cid},
                )

    _ = pid
    run_async(scenario)


def test_memory_ledger_is_append_only(api: dict[str, Any], migrated_engine: Any) -> None:
    _project(api)
    rid = _write(api, [_event()]).json()["records"][0]["record_id"]

    async def scenario() -> None:
        async with migrated_engine.connect() as conn:
            lid = (
                await conn.execute(
                    text("SELECT id FROM brain.memory_ledger WHERE :rid = ANY(record_ids) LIMIT 1"),
                    {"rid": rid},
                )
            ).scalar_one()
        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE brain.memory_ledger SET action = 'read' WHERE id = :id"),
                    {"id": lid},
                )
        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM brain.memory_ledger WHERE id = :id"), {"id": lid}
                )

    run_async(scenario)


# --- rollback: a mid-batch failure persists nothing (DoD) -------------------


def test_mid_batch_failure_rolls_back_everything(api: dict[str, Any], migrated_engine: Any) -> None:
    marker = f"rollback-{uuid.uuid4().hex[:10]}"
    ledger_task = str(uuid.uuid4())  # isolates this batch's write ledger rows
    # Event 1 is valid (org); event 2 references a non-existent project → the FK
    # violation must roll back the WHOLE batch, including event 1's rows.
    r = _write(
        api,
        [
            _event(title=marker),
            _event(scope=f"project:{uuid.uuid4()}", title=marker),
        ],
        source_agent=marker,
        task_id=ledger_task,
    )
    assert r.status_code == 422, r.text

    async def check() -> None:
        async with migrated_engine.connect() as conn:
            records = await conn.execute(
                text("SELECT count(*) FROM brain.memory_records WHERE source_agent = :m"),
                {"m": marker},
            )
            chunks = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.memory_chunks c JOIN brain.memory_records r "
                    "ON r.id = c.record_id WHERE r.source_agent = :m"
                ),
                {"m": marker},
            )
            ledger = await conn.execute(
                text("SELECT count(*) FROM brain.memory_ledger WHERE task_id = :t"),
                {"t": ledger_task},
            )
        assert int(records.scalar_one()) == 0
        assert int(chunks.scalar_one()) == 0
        assert int(ledger.scalar_one()) == 0  # no partial writes anywhere

    run_async(check)


def test_write_ledger_actor_is_the_authenticated_principal_not_body(
    api: dict[str, Any], migrated_engine: Any
) -> None:
    """The append-only ledger's actor is the authenticated caller, never the
    spoofable ``source_agent`` in the body (TITAN-BRN-005 audit integrity). The
    record still records the declared authorship separately."""
    written = _write(
        api, [_event()], source_agent="impersonated_security_agent", token="operator"
    ).json()["records"]
    rid = written[0]["record_id"]
    operator_sub = str(api["user_ids"]["operator"])

    async def check() -> None:
        async with migrated_engine.connect() as conn:
            led = await conn.execute(
                text(
                    "SELECT actor_agent, detail FROM brain.memory_ledger "
                    "WHERE :rid = ANY(record_ids) AND action = 'write'"
                ),
                {"rid": rid},
            )
            row = led.one()
            rec = await conn.execute(
                text("SELECT source_agent FROM brain.memory_records WHERE id = :id"), {"id": rid}
            )
        # Ledger actor = the authenticated operator, NOT the body's source_agent.
        assert row.actor_agent == operator_sub
        assert row.actor_agent != "impersonated_security_agent"
        # The record still keeps the declared authorship (application data).
        assert rec.scalar_one() == "impersonated_security_agent"

    run_async(check)


# --- ledger: exactly 2N rows for N writes + N reads (DoD) -------------------


def test_n_writes_and_n_reads_produce_exactly_2n_ledger_rows(
    api: dict[str, Any], migrated_engine: Any
) -> None:
    n = 5
    written = _write(api, [_event(title=f"m{i}") for i in range(n)]).json()["records"]
    record_ids = [rec["record_id"] for rec in written]
    assert len(record_ids) == n

    for rid in record_ids:  # exactly one read per record
        assert (
            api["client"]
            .get(f"/api/v1/memory/records/{rid}", headers=api["token_for"]("viewer"))
            .status_code
            == 200
        )

    async def count() -> tuple[int, int]:
        ids = [uuid.UUID(r) for r in record_ids]
        async with migrated_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT action, count(*) FROM brain.memory_ledger "
                    "WHERE record_ids && CAST(:ids AS uuid[]) GROUP BY action"
                ),
                {"ids": ids},
            )
            counts = {action: int(c) for action, c in result.all()}
        return counts.get("write", 0), counts.get("read", 0)

    writes, reads = run_async(count)
    assert writes == n and reads == n  # N per action, 2N total
