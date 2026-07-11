"""The nightly decay + curation job (Vol 5 §9, TITAN-BRN-018/019/020) — private
to the `memory` module.

Recomputes decay for every active record (§9.1), then applies the §9.2 pruning
tiers, protected classes exempt (TITAN-BRN-019):

- **Demote** (decay < 0.35): flag ``demoted`` — excluded from default retrieval
  (the PostgreSQL hydration/lexical filters, §6.1) until deep search or an access
  restores it. Non-destructive, applied automatically.
- **Archive** (decay < 0.15 for ≥ 30 consecutive days): ``status='archived'`` +
  the chunks' Qdrant vectors dropped through the §5.1 pipeline; the canonical
  text stays in PostgreSQL. Applied automatically.
- **Compact** (archived episodic > 180 days): body moved to
  ``brain.memory_bodies_cold``, live body replaced by its summary line.
  DESTRUCTIVE — gated: above 100 records per run it needs Memory-Agent approval.

Runs are dry-run-first: the plan is written to the ledger before anything is
applied, and every applied transition appends its own ``prune`` ledger row
(TITAN-BRN-018). Delete (§9.2) is never automatic — retention/human only.
"""

import json
import uuid
from collections.abc import Sequence

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.memory import repository as repo

logger = structlog.get_logger("titan.memory.curation")

# §9.2: destructive tiers (Compact/Delete) above this count need Memory-Agent
# approval; below it the job proceeds.
DESTRUCTIVE_APPROVAL_LIMIT = 100


class CurationReport(BaseModel):
    """What one curation run planned and (unless dry-run) did. Counts only — the
    ledger holds the record ids; this summary carries no memory content
    (TITAN-OPS-016)."""

    model_config = ConfigDict(extra="forbid")

    scanned: int
    planned_demote: int
    planned_archive: int
    planned_compact: int
    demoted: int = 0
    archived: int = 0
    compacted: int = 0
    # Compact records skipped because the run exceeded the approval limit
    # without ``approve_destructive`` (TITAN-BRN-018 dry-run-first gate).
    destructive_deferred: int = 0
    dry_run: bool


async def run_curation(
    engine: AsyncEngine,
    *,
    actor_agent: str,
    dry_run: bool = True,
    approve_destructive: bool = False,
) -> CurationReport:
    """Run one §9 curation cycle in a single transaction. Recompute decay, plan
    the tiers, write the plan to the ledger, and — unless ``dry_run`` — apply
    Demote + Archive automatically and Compact if within the approval limit (or
    approved). Every applied tier appends its own ``prune`` ledger row."""
    async with engine.begin() as conn:
        scanned = await repo.recompute_decay_scores(conn)
        archive_ids = await repo.archive_candidates(conn)
        demote_ids = await repo.demote_candidates(conn, archive_ids)
        compact_ids = await repo.compact_candidates(conn)

        # Dry-run-first: the plan is always written to the ledger before any
        # mutation, so an operator can audit what a run WOULD do (§9.2).
        await repo.append_ledger(
            conn,
            actor_agent=actor_agent,
            task_id=None,
            action="prune",
            record_ids=[],
            detail=json.dumps(
                {
                    "tier": "plan",
                    "dry_run": dry_run,
                    "demote": len(demote_ids),
                    "archive": len(archive_ids),
                    "compact": len(compact_ids),
                }
            ),
        )
        report = CurationReport(
            scanned=scanned,
            planned_demote=len(demote_ids),
            planned_archive=len(archive_ids),
            planned_compact=len(compact_ids),
            dry_run=dry_run,
        )
        if dry_run:
            return report

        # Demote + Archive are non-destructive-enough to apply automatically
        # (TITAN-BRN-018): a demoted record is only hidden, an archived record
        # keeps its full text and re-embeds on restore.
        await repo.mark_demoted(conn, demote_ids)
        if demote_ids:
            await _log_tier(conn, actor_agent, "demote", demote_ids)
            report.demoted = len(demote_ids)
        await repo.mark_archived(conn, archive_ids)
        if archive_ids:
            await _log_tier(conn, actor_agent, "archive", archive_ids)
            report.archived = len(archive_ids)

        # Compact is destructive: gate it above the approval limit.
        if (
            compact_ids
            and len(compact_ids) > DESTRUCTIVE_APPROVAL_LIMIT
            and not approve_destructive
        ):
            report.destructive_deferred = len(compact_ids)
            logger.warning(
                "memory.curation_compact_deferred",
                count=len(compact_ids),
                detail="Compact exceeds the approval limit; set approve_destructive to proceed.",
            )
        elif compact_ids:
            await repo.compact_records(conn, compact_ids)
            await _log_tier(conn, actor_agent, "compact", compact_ids)
            report.compacted = len(compact_ids)
        return report


async def _log_tier(
    conn: AsyncConnection, actor_agent: str, tier: str, ids: Sequence[uuid.UUID]
) -> None:
    """One ``prune`` ledger row per applied tier, carrying the affected record
    ids — every transition is audited (TITAN-BRN-018)."""
    await repo.append_ledger(
        conn,
        actor_agent=actor_agent,
        task_id=None,
        action="prune",
        record_ids=list(ids),
        detail=json.dumps({"tier": tier, "count": len(ids)}),
    )


__all__ = ["DESTRUCTIVE_APPROVAL_LIMIT", "CurationReport", "run_curation"]
