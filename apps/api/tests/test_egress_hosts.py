"""Admin egress opt-in (Phase 23; SEC-033/043) — the project_egress_hosts repo
and the forward proxy's DB relaxation on the deny path. ``schema``-marked."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from titan.db import uuid7
from titan.tools.egress.hosts import (
    add_approved_host,
    is_host_approved,
    list_approved_hosts,
    remove_approved_host,
)
from titan.tools.egress.service import build_forward_proxy
from titan.tools.egress.tokens import TokenClaims

from .conftest import run_async

pytestmark = pytest.mark.schema


async def _seed_project(engine: Any) -> tuple[uuid.UUID, uuid.UUID]:
    project_id, user_id = uuid7(), uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'u', 'local', :s)"
            ),
            {"id": user_id, "e": f"u-{user_id}@titan.local", "s": str(user_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'brief', :by)"
            ),
            {"id": project_id, "n": f"proj-{project_id}", "by": user_id},
        )
    return project_id, user_id


def _claims(project_id: uuid.UUID) -> TokenClaims:
    return TokenClaims(
        project_id=project_id,
        task_id=None,
        agent_id="sandbox",
        git_host=None,
        approved_hosts=(),
        expires_at=0.0,
    )


def test_host_crud_and_proxy_relaxation(migrated_engine: Any) -> None:
    async def scenario() -> None:
        project_id, user_id = await _seed_project(migrated_engine)
        async with migrated_engine.begin() as conn:
            assert await is_host_approved(conn, project_id, "docs.example.com") is False
            # Idempotent + case/dot-normalized opt-in.
            await add_approved_host(conn, project_id, "Docs.Example.com.", approved_by=user_id)
            await add_approved_host(conn, project_id, "docs.example.com", approved_by=user_id)
        async with migrated_engine.connect() as conn:
            hosts = await list_approved_hosts(conn, project_id)
            assert [h.host for h in hosts] == ["docs.example.com"]
            assert await is_host_approved(conn, project_id, "docs.example.com") is True

        # The forward proxy's deny-path DB relaxation now permits the host.
        proxy = build_forward_proxy(migrated_engine, secret="s")
        claims = _claims(project_id)
        assert await proxy._permitted("docs.example.com", claims) is True
        assert await proxy._permitted("evil.example", claims) is False
        # A registry host is still permitted offline (no DB needed).
        assert await proxy._permitted("pypi.org", claims) is True

        async with migrated_engine.begin() as conn:
            assert await remove_approved_host(conn, project_id, "docs.example.com") is True
        async with migrated_engine.connect() as conn:
            assert await is_host_approved(conn, project_id, "docs.example.com") is False

    run_async(scenario)
