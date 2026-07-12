"""Phase 43 — the versioned constitution artifact (Vol 6 §6).

Version 1 is seeded verbatim from §6 by migration 0021; agents/gates retrieve it
into context and G5 records the version judged against. The artifact is
append-only (a revision is a new version), and its checksum lets a reader confirm
the retrieved copy is byte-intact.
"""

import hashlib
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.constitution import service as constitution

pytestmark = pytest.mark.schema


def test_active_constitution_is_seeded_and_intact(api: dict[str, Any]) -> None:
    # Read version 1 explicitly: the session DB is shared and versions are
    # append-only, so another test may have committed a later active version.
    async def _read() -> tuple[Any, Any]:
        async with api["engine"].connect() as conn:
            return (
                await constitution.get_constitution(conn, 1),
                await constitution.active_constitution(conn),
            )

    v1, active = run_async(_read)
    assert v1.version == 1 and v1.source_ref == "Volume 6 §6"
    assert "Engineering Standards Constitution" in v1.body
    # The checksum matches the body → the retrieved text is byte-intact.
    assert v1.checksum == hashlib.sha256(v1.body.encode("utf-8")).hexdigest()
    # The active version is always present and self-consistent.
    assert active.version >= 1
    assert active.checksum == hashlib.sha256(active.body.encode("utf-8")).hexdigest()


def test_constitution_endpoint_returns_seeded_version(api: dict[str, Any]) -> None:
    # Version 1 is immutable, so its §6 content is asserted directly.
    r = api["client"].get("/api/v1/constitution/1", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and "TITAN-WFL-022" in r.json()["body"]
    # The active endpoint always resolves to some committed version.
    assert (
        api["client"].get("/api/v1/constitution", headers=api["token_for"]("owner")).status_code
        == 200
    )


def test_constitution_version_lookup_and_404(api: dict[str, Any]) -> None:
    assert (
        api["client"].get("/api/v1/constitution/1", headers=api["token_for"]("owner")).status_code
        == 200
    )
    assert (
        api["client"].get("/api/v1/constitution/99", headers=api["token_for"]("owner")).status_code
        == 404
    )


def test_constitution_history_lists_versions(api: dict[str, Any]) -> None:
    r = api["client"].get("/api/v1/constitution/versions", headers=api["token_for"]("owner"))
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == sorted(
        (v["version"] for v in versions), reverse=True
    )
    assert any(v["version"] == 1 for v in versions)


def test_constitution_requires_authentication(api: dict[str, Any]) -> None:
    assert api["client"].get("/api/v1/constitution").status_code == 401


def test_commit_revision_appends_new_version(api: dict[str, Any]) -> None:
    async def _revise() -> tuple[int, int]:
        async with api["engine"].begin() as conn:
            before = (await constitution.active_constitution(conn)).version
            out = await constitution.commit_revision(
                conn, title="v2", body="## amended standards", source_ref="governance"
            )
            return before, out.version

    before, after = run_async(_revise)
    assert after == before + 1  # a revision is a new version, never an overwrite


def test_constitution_versions_are_append_only(api: dict[str, Any]) -> None:
    async def _mutate() -> str:
        try:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text("UPDATE constitution_versions SET title = 'x' WHERE version = 1")
                )
            return "no-error"
        except Exception as exc:  # noqa: BLE001 — asserting the trigger fires
            return type(exc).__name__

    assert run_async(_mutate) != "no-error"
