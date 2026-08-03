"""Evolution service façade (Vol 10 §2, TITAN-EVO-001/002).

The composition-root operations over the ``evolution_artifacts`` ledger:

  * :func:`sync_manifest_to_ledger` — reconcile the DB with the git-versioned
    manifest at startup, so the immutable approval references (Git SHA + path +
    content hash + approver) are queryable (TITAN-EVO-002). Never writes prompt
    text.
  * :func:`approve_artifact` — the human-gated approval ACTION: record an
    approval row + write the audit event with actor, artifact hash, and diff link
    (TITAN-EVO-001c). The RBAC ``evolution:approve`` check is enforced at the
    route; this records the outcome.
  * :func:`list_artifacts` / :func:`active_prompt_versions` — read surface for the
    dashboard ("which prompt version served a task", TITAN-EVO-015c).

This module never resolves filesystem paths itself (that stays with the agent
config resolver); it takes a built :class:`EvolutionResolver`, so it imports no
domain module.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from titan.audit.service import AuditAction, AuditClient, Outcome
from titan.evolution import gitref, repository
from titan.evolution.manifest import ArtifactEntry
from titan.evolution.resolver import EvolutionResolver


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def sync_manifest_to_ledger(
    engine: AsyncEngine, resolver: EvolutionResolver, *, commit_sha: str | None = None
) -> int:
    """Insert every manifest-declared artifact into the ledger (idempotent).

    ``commit_sha`` defaults to the current HEAD (best-effort; ``None`` if git is
    unavailable). Called once at startup so a fresh DB has the genesis approval
    references TITAN-EVO-002 requires, without a manual seed step."""
    if commit_sha is None:
        try:
            commit_sha = gitref.head_sha(resolver.repo_root)
        except gitref.GitError:
            commit_sha = None
    entries: list[ArtifactEntry] = resolver.manifest.all_entries()
    count = 0
    async with engine.begin() as conn:
        for entry in entries:
            await repository.record_artifact(
                conn,
                kind=entry.kind,
                ref=entry.ref,
                path=entry.path,
                content_sha256=entry.sha256,
                commit_sha=commit_sha,
                semver=entry.semver,
                ep=entry.ep,
                approved_by=_parse_uuid(entry.approved_by) if entry.approved else None,
                approved_at=_parse_ts(entry.approved_at) if entry.approved else None,
                diff_url=entry.diff_url,
            )
            count += 1
    return count


async def approve_artifact(
    engine: AsyncEngine,
    audit: AuditClient,
    *,
    kind: str,
    ref: str,
    path: str,
    content_sha256: str,
    semver: str,
    ep: str,
    diff_url: str,
    approver_user_id: uuid.UUID,
    actor: str,
    commit_sha: str | None = None,
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Record a human approval of ``(path, content_sha256)`` and audit it.

    The RBAC ``evolution:approve`` gate is enforced by the route before this runs
    (TITAN-EVO-001). The DB write + audit event are atomic (shared ``conn``) so an
    approval is never recorded without its tamper-evident audit line, and vice
    versa."""
    approved_at = datetime.now(UTC)
    async with engine.begin() as conn:
        await repository.record_artifact(
            conn,
            kind=kind,
            ref=ref,
            path=path,
            content_sha256=content_sha256,
            commit_sha=commit_sha,
            semver=semver,
            ep=ep,
            approved_by=approver_user_id,
            approved_at=approved_at,
            diff_url=diff_url,
        )
        await audit.record(
            actor=actor,
            action=AuditAction.EVOLUTION_APPROVE,
            resource=path,
            outcome=Outcome.SUCCESS,
            source_ip=source_ip,
            conn=conn,
            detail={
                "kind": kind,
                "ref": ref,
                "artifact_hash": content_sha256,
                "ep": ep,
                "semver": semver,
                "diff_url": diff_url,
                "commit_sha": commit_sha,
            },
        )
        row = await repository.get_artifact(conn, path=path, content_sha256=content_sha256)
    assert row is not None  # just inserted in this txn
    # EVO-013: a merged/approved EP annotates the RCRs it remediates. Audit which RCRs
    # this approval declared fixed — the free-form `ep` key means one approval can flip
    # multiple RCRs to 'remediated', so that set must be traceable to the approver.
    if ep:
        remediated = await repository.annotate_rcrs_for_merged_ep(engine, ep)
        if remediated:
            await audit.record(
                actor=actor,
                action=AuditAction.EVOLUTION_RCR_UPDATE,
                resource=f"ep:{ep}",
                outcome=Outcome.SUCCESS,
                source_ip=source_ip,
                detail={"op": "auto_remediate_on_merge", "ep": ep, "rcr_ids": remediated},
            )
    return row


async def list_artifacts(
    engine: AsyncEngine,
    *,
    kind: str | None = None,
    ref: str | None = None,
    approved_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return await repository.list_artifacts(
            conn, kind=kind, ref=ref, approved_only=approved_only, limit=limit
        )


def active_prompt_versions(resolver: EvolutionResolver) -> dict[str, dict[str, str]]:
    return resolver.active_prompt_versions()


async def list_investigations(
    engine: AsyncEngine, *, status: str = "open", limit: int = 100
) -> list[dict[str, Any]]:
    return await repository.list_investigations(engine, status=status, limit=limit)


__all__ = [
    "active_prompt_versions",
    "approve_artifact",
    "list_artifacts",
    "list_investigations",
    "sync_manifest_to_ledger",
]
