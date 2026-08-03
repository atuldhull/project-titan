"""The evolution_artifacts ledger + human-gated approve action (Phase 83).

Service-level tests over the real schema: the manifest→ledger sync stores only
immutable references (TITAN-EVO-002), and an approval writes both the ledger row
and the tamper-evident audit event with actor + artifact hash + diff link
(TITAN-EVO-001c).
"""

import uuid
from typing import Any

import pytest

from tests.conftest import run_async
from titan.agents.service import build_evolution_resolver
from titan.audit import repository as audit_repo
from titan.audit.service import build_audit_client
from titan.evolution import service as evo
from titan.settings import Settings

pytestmark = pytest.mark.schema


def _resolver() -> Any:
    return build_evolution_resolver(Settings())


def test_sync_stores_only_references_for_every_manifest_entry(migrated_engine: Any) -> None:
    async def scenario() -> None:
        resolver = _resolver()
        count = await evo.sync_manifest_to_ledger(migrated_engine, resolver)
        expected = len(resolver.manifest.all_entries())  # base + routing + 16 prompts
        assert count == expected
        rows = await evo.list_artifacts(migrated_engine, limit=500)
        assert len(rows) >= expected
        # Genesis entries are approved; the ledger carries the content hash, never text.
        by_ref = {r["ref"]: r for r in rows}
        reviewer = by_ref["reviewer"]
        assert reviewer["approved_by"] is not None and reviewer["approved_at"] is not None
        assert reviewer["content_sha256"] == resolver.manifest.prompts["reviewer"].sha256
        assert "path" in reviewer and "text" not in reviewer  # references only

    run_async(scenario)


def test_sync_is_idempotent(migrated_engine: Any) -> None:
    async def scenario() -> None:
        resolver = _resolver()
        await evo.sync_manifest_to_ledger(migrated_engine, resolver)
        first = await evo.list_artifacts(migrated_engine, limit=500)
        await evo.sync_manifest_to_ledger(migrated_engine, resolver)  # again
        second = await evo.list_artifacts(migrated_engine, limit=500)
        # UNIQUE(path, content_sha256): re-sync updates in place, never duplicates.
        assert len(second) == len(first)

    run_async(scenario)


def test_approve_records_ledger_row_and_audit_event(migrated_engine: Any) -> None:
    async def scenario() -> None:
        resolver = _resolver()
        entry = resolver.manifest.prompts["security"]
        audit = build_audit_client(migrated_engine)
        approver = uuid.uuid4()

        row = await evo.approve_artifact(
            migrated_engine,
            audit,
            kind="prompt",
            ref="security",
            path=entry.path,
            content_sha256=entry.sha256,
            semver=entry.semver,
            ep="EP-0007",
            diff_url="https://example.test/pr/7",
            approver_user_id=approver,
            actor=f"user:{approver}",
        )
        assert row["approved_by"] == str(approver)
        assert row["content_sha256"] == entry.sha256

        # The approval is in the immutable audit ledger with actor + artifact hash.
        async with migrated_engine.connect() as conn:
            events = await audit_repo.search(conn, action="evolution.approve", limit=10)
        assert events, "no evolution.approve audit event written"
        hashes = [
            (e.detail if isinstance(e.detail, dict) else __import__("json").loads(e.detail)).get(
                "artifact_hash"
            )
            for e in events
        ]
        assert entry.sha256 in hashes

    run_async(scenario)
