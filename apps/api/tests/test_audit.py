"""Phase 73 (Vol 8 §10): the immutable, hash-chained audit ledger.

DoD: UPDATE/DELETE on audit_events as the app role fail at the DB layer (SEC-091);
corrupting one row breaks chain verification (SEC-091); a catalog spot-check
exercises every §10.2 event class and asserts a schema-valid row (SEC-090);
secrets never land in the detail (SEC-092); the retention job prunes aged sealed
rows (SEC-093); the search + verify endpoints are audit:read-gated (SEC-094).

The audit_events chain is GLOBAL and the schema DB is shared across the session,
so tests that tamper restore, and the retention test truncates for isolation.
"""

import datetime
import json
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import text

from titan.audit import repository as audit_repo
from titan.audit import service as audit_service
from titan.audit.service import AuditAction, AuditClient, Outcome

from .conftest import run_async

pytestmark = pytest.mark.schema

_UTC = datetime.UTC


def _client(api: dict[str, Any], clock: Any = None) -> AuditClient:
    return AuditClient(api["engine"], clock=clock)


def _record(client: AuditClient, **kw: Any) -> audit_repo.AppendedEvent:
    """Append one event synchronously (a helper so loop bodies don't need a
    loop-variable-capturing lambda — ruff B023 vs mypy lambda inference)."""
    return cast("audit_repo.AppendedEvent", run_async(lambda: client.record(**kw)))


def _row(api: dict[str, Any], event_id: uuid.UUID) -> Any:
    async def q() -> Any:
        async with api["engine"].connect() as conn:
            return (
                await conn.execute(
                    text(f"SELECT {audit_repo._COLS} FROM audit_events WHERE id = :i"),
                    {"i": event_id},
                )
            ).one_or_none()

    return run_async(q)


# --- SEC-091: immutability at the DB layer ---------------------------------


def test_update_and_delete_are_rejected_at_the_db_layer(api: dict[str, Any]) -> None:
    client = _client(api)
    appended = run_async(
        lambda: client.record(
            actor="tester", action=AuditAction.DEPLOY, resource="stack:x", outcome=Outcome.SUCCESS
        )
    )

    async def do_update() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text("UPDATE audit_events SET actor = 'x' WHERE id = :i"), {"i": appended.id}
            )

    async def do_delete() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(text("DELETE FROM audit_events WHERE id = :i"), {"i": appended.id})

    with pytest.raises(Exception, match="append-only"):
        run_async(do_update)
    with pytest.raises(Exception, match="append-only"):
        run_async(do_delete)
    # The row is untouched.
    assert _row(api, appended.id).actor == "tester"


# --- SEC-091: a corrupted row breaks chain verification --------------------


def test_corrupting_a_row_breaks_then_restore_repairs_verification(api: dict[str, Any]) -> None:
    client = _client(api)
    ids = [
        _record(
            client,
            actor=f"a{i}",
            action=AuditAction.TASK_LIFECYCLE,
            resource=f"task:{i}",
            outcome=Outcome.SUCCESS,
        )
        for i in range(3)
    ]
    target = ids[1]

    def toggle_and_set(actor: str) -> None:
        async def _do() -> None:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_immutable")
                )
                await conn.execute(
                    text("UPDATE audit_events SET actor = :a WHERE id = :i"),
                    {"a": actor, "i": target.id},
                )
                await conn.execute(
                    text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_immutable")
                )

        run_async(_do)

    # Tamper (bypassing the trigger, as a superuser could) → the row's stored
    # event_hash no longer matches its content → verification flags THIS row.
    toggle_and_set("TAMPERED")
    assert run_async(lambda: audit_service.verify_chain(api["engine"])) == target.seq
    # Restore the original content → the recompute matches again → chain intact.
    toggle_and_set("a1")
    assert run_async(lambda: audit_service.verify_chain(api["engine"])) is None


# --- SEC-090: catalog spot-check over every §10.2 event class ---------------


def test_catalog_every_action_produces_a_schema_valid_chained_row(api: dict[str, Any]) -> None:
    client = _client(api)
    for action in AuditAction:
        appended = _record(
            client,
            actor="catalog",
            action=action,
            resource=f"res:{action.value}",
            outcome=Outcome.SUCCESS,
            project_id=uuid.uuid4(),
            source_ip="203.0.113.7",
            detail={"note": action.value},
        )
        row = _row(api, appended.id)
        assert row is not None, action
        # SEC-090 envelope present + typed.
        assert row.action == action.value
        assert row.outcome == "success"
        assert row.actor == "catalog"
        assert row.resource == f"res:{action.value}"
        assert row.source_ip == "203.0.113.7"
        assert len(row.event_hash) == 64 and row.event_hash == appended.event_hash
        assert row.prev_hash and row.seq > 0


# --- SEC-092: no secret values in the detail --------------------------------


def test_secret_values_are_redacted_from_detail(api: dict[str, Any]) -> None:
    client = _client(api)
    # A high-entropy secret-looking token + an AWS-key-shaped string.
    secret = "AKIAIOSFODNN7EXAMPLE"
    appended = run_async(
        lambda: client.record(
            actor="svc",
            action=AuditAction.SECRET_LIFECYCLE,
            resource="secret:prod-db",
            outcome=Outcome.SUCCESS,
            detail={"value": secret, "note": "rotated prod-db"},
        )
    )
    row = _row(api, appended.id)
    detail = row.detail if isinstance(row.detail, dict) else json.loads(row.detail)
    # The raw secret must NOT appear anywhere in the stored detail (SEC-092).
    assert secret not in str(detail)


# --- SEC-093: retention prunes aged, SEALED rows -----------------------------


def test_retention_prunes_only_aged_sealed_rows(api: dict[str, Any]) -> None:
    # Isolate: a fresh chain (TRUNCATE bypasses the row-level immutability trigger).
    async def truncate() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(text("TRUNCATE audit_events, audit_seals"))

    run_async(truncate)

    now = datetime.datetime(2026, 7, 1, tzinfo=_UTC)
    old = now - datetime.timedelta(days=500)  # past the 400-day online window
    old_client = AuditClient(api["engine"], clock=lambda: old)
    fresh_client = AuditClient(api["engine"], clock=lambda: now)

    aged = run_async(
        lambda: old_client.record(
            actor="old", action=AuditAction.TASK_LIFECYCLE, resource="t", outcome=Outcome.SUCCESS
        )
    )
    fresh = run_async(
        lambda: fresh_client.record(
            actor="new", action=AuditAction.TASK_LIFECYCLE, resource="t", outcome=Outcome.SUCCESS
        )
    )

    # Before sealing, retention removes NOTHING (an unsealed row is never pruned).
    removed_unsealed = run_async(
        lambda: audit_service.apply_retention(api["engine"], now=now, online_days=400)
    )
    assert removed_unsealed == 0
    assert _row(api, aged.id) is not None

    # Seal past days (the 500-day-old day) with fakes, then retention prunes it.
    archive: dict[str, bytes] = {}
    report = run_async(
        lambda: audit_service.seal_days(
            api["engine"],
            encrypt=lambda pt: b"AGE" + pt,  # stand-in for age encryption
            archive=lambda ref, data: archive.__setitem__(ref, data),
            now=now,
        )
    )
    assert report.days_sealed >= 1 and report.events_sealed >= 1
    assert any(ref.endswith(".age") for ref in archive)  # a sealed segment was written

    removed = run_async(
        lambda: audit_service.apply_retention(api["engine"], now=now, online_days=400)
    )
    assert removed == 1  # the aged, now-sealed row
    assert _row(api, aged.id) is None  # pruned from the hot table
    assert _row(api, fresh.id) is not None  # inside the window → kept
    # The remaining online chain still verifies (verify trusts the first online
    # row's prev_hash — it followed the pruned+archived row).
    assert run_async(lambda: audit_service.verify_chain(api["engine"])) is None


def test_verify_detects_an_unsealed_prefix_deletion(api: dict[str, Any]) -> None:
    """The review's finding: deleting a prefix (or wiping the ledger) that is NOT
    backed by a seal must NOT read as intact. verify anchors the first online
    row's prev_hash to the seal tip (or genesis); an unsealed prefix delete leaves
    it pointing at a vanished row → detected. Re-break: trust the first row's
    prev_hash and this passes silently (the hole)."""

    async def truncate() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(text("TRUNCATE audit_events, audit_seals"))

    run_async(truncate)
    client = _client(api)
    events = [
        _record(
            client,
            actor=f"e{i}",
            action=AuditAction.AUTHZ_DENIED,
            resource=f"r{i}",
            outcome=Outcome.DENIED,
        )
        for i in range(3)
    ]
    assert run_async(lambda: audit_service.verify_chain(api["engine"])) is None

    # Delete the FIRST row via the retention GUC but WITHOUT sealing it (evidence
    # destruction, not archival). The new first online row now dangles.
    async def wipe_prefix() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(text("SET LOCAL titan.audit_retention = 'on'"))
            await conn.execute(
                text("DELETE FROM audit_events WHERE seq <= :s"), {"s": events[0].seq}
            )

    run_async(wipe_prefix)
    # The chain no longer verifies — the deletion is surfaced at the new first row.
    assert run_async(lambda: audit_service.verify_chain(api["engine"])) == events[1].seq


# --- SEC-094: search + verify endpoints, audit:read-gated -------------------


def test_audit_endpoints_require_audit_read(api: dict[str, Any]) -> None:
    client = _client(api)
    run_async(
        lambda: client.record(
            actor="endpoint-probe",
            action=AuditAction.DEPLOY,
            resource="stack:e2e",
            outcome=Outcome.SUCCESS,
        )
    )
    c = api["client"]
    # audit is SENSITIVE — a viewer's *:read does NOT reach it.
    assert c.get("/api/v1/audit", headers=api["token_for"]("viewer")).status_code == 403
    assert c.get("/api/v1/audit/verify", headers=api["token_for"]("operator")).status_code == 403
    # Owner (audit:read via "*") can search + verify.
    found = c.get("/api/v1/audit?actor=endpoint-probe", headers=api["token_for"]("owner"))
    assert found.status_code == 200
    assert any(e["action"] == "deploy" for e in found.json())
    verified = c.get("/api/v1/audit/verify", headers=api["token_for"]("owner"))
    assert verified.status_code == 200 and "intact" in verified.json()
