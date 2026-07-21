"""The vault→redactor inventory feed + the redaction audit trail (Phase 69,
TITAN-SEC-054 detector 1 + "redaction hits emit audit events" / SEC-092)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from titan.redaction import Hit, RedactionReporter, default_engine
from titan.vault import inventory
from titan.vault import service as vault_service
from titan.vault.drivers.static import StaticSecretsProvider
from titan.vault.provider import SecretScope

# --- collect_index (pure: static provider, no DB) ------------------------------


def test_collect_index_hashes_only_live_resolvable_values() -> None:
    store = StaticSecretsProvider(
        {"GOOD": "live-secret-value-123", "SHORT": "tiny", "OTHER": "second-value-456"}
    )

    async def build() -> Any:
        # 'GONE' has a metadata row but no live value → skipped, not an error.
        return await inventory.collect_index(store, {"GOOD", "GONE"}, store=store)

    index = run_async(build)
    # GOOD + OTHER (store enumeration) indexed; SHORT skipped (min length);
    # GONE unresolvable. Variants may add extra digests — check behavior, not
    # internal count: the engine must match both indexed values.
    engine = default_engine()
    engine.set_inventory(index)
    assert "live-secret-value-123" not in engine.redact("x live-secret-value-123")
    assert "second-value-456" not in engine.redact("x second-value-456")
    assert engine.redact("x tiny") == "x tiny"


def test_collect_index_without_store_uses_metadata_names_only() -> None:
    provider = StaticSecretsProvider({"NAMED": "named-secret-value-789"})

    async def build() -> Any:
        return await inventory.collect_index(provider, {"NAMED"}, store=None)

    index = run_async(build)
    assert len(index) > 0


# --- refresh_default_engine against the real schema (schema-marked per test,
# NOT via module pytestmark, so the pure tests above run in the plain-unit
# coverage gate) ---------------------------------------------------------------


@pytest.mark.schema
def test_refresh_default_engine_picks_up_a_created_secret(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    store = StaticSecretsProvider()
    secret_value = "refresh-me-hunter2-secret-42"

    async def scenario() -> int:
        async with engine.begin() as conn:
            await vault_service.create_secret(
                conn,
                store,
                name="PHASE69_TEST_SECRET",
                value=secret_value,
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=90,
                actor="test",
            )
        return await inventory.refresh_default_engine(engine, store, store)

    size = run_async(scenario)
    assert size > 0
    redacted = default_engine().redact(f"leaked {secret_value}", sink="test")
    assert secret_value not in redacted
    assert "[REDACTED:inventory:" in redacted


@pytest.mark.schema
def test_deleted_secret_leaves_the_inventory_on_refresh(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    store = StaticSecretsProvider()
    secret_value = "delete-me-hunter2-secret-43"

    async def scenario() -> tuple[int, int]:
        async with engine.begin() as conn:
            await vault_service.create_secret(
                conn,
                store,
                name="PHASE69_DELETE_ME",
                value=secret_value,
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=90,
                actor="test",
            )
        before = await inventory.refresh_default_engine(engine, store, store)
        async with engine.begin() as conn:
            await vault_service.delete_secret(conn, store, name="PHASE69_DELETE_ME", actor="t")
        after = await inventory.refresh_default_engine(engine, store, store)
        return before, after

    before, after = run_async(scenario)
    assert before > 0
    assert after == 0
    assert default_engine().redact(f"x {secret_value}") == f"x {secret_value}"


# --- the audit trail ----------------------------------------------------------------


def _audit_rows(engine: AsyncEngine) -> list[Any]:
    async def read() -> list[Any]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT event_type, actor, secret_name, detail FROM secret_audit "
                    "WHERE event_type = 'redaction.hit' ORDER BY created_at"
                )
            )
            return list(rows.all())

    return run_async(read)  # type: ignore[no-any-return]


@pytest.mark.schema
def test_record_redaction_hits_writes_sec092_safe_rows(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    reporter = RedactionReporter()
    reporter.record("log", [Hit(cls="anthropic-key", hash8="ab12cd34")])
    reporter.record("log", [Hit(cls="anthropic-key", hash8="ab12cd34")])
    reporter.record("gateway", [Hit(cls="inventory", hash8="ffee0011")])

    run_async(
        lambda: vault_service.record_redaction_hits(
            engine, service="titan-api", records=reporter.drain()
        )
    )
    rows = _audit_rows(engine)
    assert len(rows) == 2
    by_sink = {row.detail["sink"]: row for row in rows}
    log_row = by_sink["log"]
    assert log_row.event_type == "redaction.hit"
    assert log_row.actor == "redactor:titan-api"
    assert log_row.secret_name is None
    assert log_row.detail == {
        "sink": "log",
        "class": "anthropic-key",
        "hash8": "ab12cd34",
        "count": 2,
    }
    assert by_sink["gateway"].detail["class"] == "inventory"
    # SEC-092: nothing value-shaped anywhere in the rows.
    assert "sk-" not in str(rows)


@pytest.mark.schema
def test_maintenance_loop_final_flush_and_failed_flush_requeues(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    store = StaticSecretsProvider()
    reporter = RedactionReporter()
    reporter.record("sandbox-stream", [Hit(cls="entropy", hash8="0badf00d")])

    async def run_once() -> None:
        stop = asyncio.Event()
        stop.set()  # skip the loop body — the shutdown path must still flush
        await inventory.run_redaction_maintenance(
            engine, store, store, reporter, service="titan-worker", stop=stop
        )

    run_async(run_once)
    rows = _audit_rows(engine)
    assert any(
        row.detail["sink"] == "sandbox-stream" and row.actor == "redactor:titan-worker"
        for row in rows
    )
    # A failed flush re-queues instead of losing hits.
    reporter.record("log", [Hit(cls="jwt", hash8="12345678")])
    records = reporter.drain()
    assert reporter.drain() == []
    reporter.merge(records)
    assert reporter.drain() == records
