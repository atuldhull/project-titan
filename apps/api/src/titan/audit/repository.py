"""audit_events data access: hash-chained append, chain verification, search,
sealing, and retention. SQLAlchemy Core over an AsyncConnection; only
``service.py`` crosses the module boundary (TITAN-BCK-001).

The GLOBAL chain serializes appends via one transaction-scoped advisory lock, so
``prev_hash`` linkage is race-free under concurrent auditing. The hash covers the
event CONTENT (event_id + envelope + detail), not the DB-set seq/created_at —
those are immutable by the block trigger, matching the tool_audit precedent.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

GENESIS_HASH = "0" * 64
_CHAIN_LOCK = "audit_events:global"

_COLS = (
    "seq, id, created_at, actor, org, project_id, task_id, action, resource, "
    "outcome, source_ip, trace_id, detail, prev_hash, event_hash"
)


def canonical_json(payload: Any) -> str:
    """Canonical JSON (sorted keys, tight separators, non-JSON scalars stringified)
    — byte-stable across processes so the hash is reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_event_hash(prev_hash: str, content: dict[str, Any]) -> str:
    """SEC-090: event_hash = sha256(prev_hash || canonical_event_json)."""
    return hashlib.sha256((prev_hash + canonical_json(content)).encode("utf-8")).hexdigest()


def _pg_safe(value: Any) -> Any:
    """Strip NULs (PostgreSQL jsonb rejects them) recursively — a hostile detail
    payload must still be auditable; the ledger write may never fail on shape."""
    if isinstance(value, str):
        return value.replace(chr(0), chr(0xFFFD))
    if isinstance(value, dict):
        return {_pg_safe(k): _pg_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_pg_safe(v) for v in value]
    return value


def _content(
    *,
    event_id: uuid.UUID,
    actor: str,
    org: uuid.UUID | None,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    action: str,
    resource: str,
    outcome: str,
    source_ip: str | None,
    trace_id: str | None,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": str(event_id),
        "actor": actor,
        "org": str(org) if org is not None else None,
        "project": str(project_id) if project_id is not None else None,
        "task": str(task_id) if task_id is not None else None,
        "action": action,
        "resource": resource,
        "outcome": outcome,
        "source_ip": source_ip,
        "trace_id": trace_id,
        "detail": detail,
    }


@dataclass(frozen=True)
class AppendedEvent:
    id: uuid.UUID
    seq: int
    event_hash: str


async def append_event(
    conn: AsyncConnection,
    *,
    event_id: uuid.UUID,
    ts: datetime.datetime,
    actor: str,
    org: uuid.UUID | None,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    action: str,
    resource: str,
    outcome: str,
    source_ip: str | None,
    trace_id: str | None,
    detail: dict[str, Any],
) -> AppendedEvent:
    """Append one chained row. Must run inside a transaction (the advisory lock is
    transaction-scoped)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:c, 0))"), {"c": _CHAIN_LOCK}
    )
    prev = await conn.execute(text("SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1"))
    prev_hash = prev.scalar_one_or_none() or GENESIS_HASH
    # Round-trip through JSON so the hashed value is byte-identical to what
    # verify_chain later reads back from JSONB; strip NULs jsonb rejects.
    safe_detail = _pg_safe(json.loads(json.dumps(detail, default=str)))
    content = _content(
        event_id=event_id,
        actor=actor,
        org=org,
        project_id=project_id,
        task_id=task_id,
        action=action,
        resource=resource,
        outcome=outcome,
        source_ip=source_ip,
        trace_id=trace_id,
        detail=safe_detail,
    )
    event_hash = compute_event_hash(prev_hash, content)
    result = await conn.execute(
        text(
            "INSERT INTO audit_events "
            "(id, actor, org, project_id, task_id, action, resource, outcome, source_ip, "
            "trace_id, detail, prev_hash, event_hash, created_at) "
            "VALUES (:id, :actor, :org, :project_id, :task_id, :action, :resource, :outcome, "
            ":source_ip, :trace_id, CAST(:detail AS jsonb), :prev_hash, :event_hash, :ts) "
            "RETURNING seq"
        ),
        {
            "id": event_id,
            "actor": actor,
            "org": org,
            "project_id": project_id,
            "task_id": task_id,
            "action": action,
            "resource": resource,
            "outcome": outcome,
            "source_ip": source_ip,
            "trace_id": trace_id,
            "detail": json.dumps(safe_detail),
            "prev_hash": prev_hash,
            "event_hash": event_hash,
            "ts": ts,
        },
    )
    return AppendedEvent(id=event_id, seq=int(result.scalar_one()), event_hash=event_hash)


def _row_content(row: Row[Any]) -> dict[str, Any]:
    detail = row.detail
    if isinstance(detail, str):
        detail = json.loads(detail)
    return _content(
        event_id=row.id,
        actor=row.actor,
        org=row.org,
        project_id=row.project_id,
        task_id=row.task_id,
        action=row.action,
        resource=row.resource,
        outcome=row.outcome,
        source_ip=row.source_ip,
        trace_id=row.trace_id,
        detail=detail,
    )


async def verify_chain(conn: AsyncConnection, *, batch: int = 1000) -> int | None:
    """Walk the global chain in seq order; return the seq of the FIRST row whose
    stored event_hash does not match the recompute, or whose prev_hash breaks the
    linkage to the prior row. None = the whole (online) chain is intact (SEC-091).

    The first ONLINE row's prev_hash is ANCHORED (not blindly trusted): it must
    equal the genesis hash, OR the tip_hash of the seal that archived the rows just
    below it. Retention (SEC-093) prunes the oldest rows to the sealed archive, so
    the online chain legitimately begins mid-way — but ONLY behind a real seal. A
    prefix delete (or whole-ledger wipe) NOT backed by a seal leaves the first
    online row's prev_hash pointing at a vanished row that matches neither genesis
    nor any seal tip → detected. Every row's own hash + forward linkage are
    verified too (mutation/reorder/mid-delete all break)."""
    first = (
        await conn.execute(text(f"SELECT {_COLS} FROM audit_events ORDER BY seq LIMIT 1"))
    ).one_or_none()
    if first is None:
        return None  # empty online table — everything archived, or nothing written
    prev_hash: str = await sealed_tip_below(conn, int(first.seq)) or GENESIS_HASH
    cursor = 0
    while True:
        rows = (
            await conn.execute(
                text(f"SELECT {_COLS} FROM audit_events WHERE seq > :s ORDER BY seq LIMIT :b"),
                {"s": cursor, "b": batch},
            )
        ).all()
        if not rows:
            return None
        for row in rows:
            if row.prev_hash != prev_hash:
                return int(row.seq)
            if compute_event_hash(row.prev_hash, _row_content(row)) != row.event_hash:
                return int(row.seq)
            prev_hash = row.event_hash
            cursor = int(row.seq)


async def search(
    conn: AsyncConnection,
    *,
    actor: str | None = None,
    action: str | None = None,
    resource: str | None = None,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
    limit: int = 100,
) -> Sequence[Row[Any]]:
    """SEC-094 admin search (actor, action, resource, time), newest first."""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if actor is not None:
        clauses.append("actor = :actor")
        params["actor"] = actor
    if action is not None:
        clauses.append("action = :action")
        params["action"] = action
    if resource is not None:
        clauses.append("resource = :resource")
        params["resource"] = resource
    if since is not None:
        clauses.append("created_at >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("created_at < :until")
        params["until"] = until
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM audit_events {where} ORDER BY seq DESC LIMIT :limit"), params
    )
    return result.all()


# --- sealing + retention (SEC-093) ----------------------------------------


async def unsealed_days_before(conn: AsyncConnection, before: datetime.date) -> list[datetime.date]:
    """UTC days with audit rows that have not yet been sealed, strictly before
    ``before`` (so the current day is never sealed mid-stream)."""
    result = await conn.execute(
        text(
            "SELECT DISTINCT (created_at AT TIME ZONE 'UTC')::date AS day FROM audit_events e "
            "WHERE (created_at AT TIME ZONE 'UTC')::date < :before "
            "AND NOT EXISTS (SELECT 1 FROM audit_seals s "
            "WHERE s.day = (e.created_at AT TIME ZONE 'UTC')::date) "
            "ORDER BY day"
        ),
        {"before": before},
    )
    return [r.day for r in result.all()]


async def events_for_day(conn: AsyncConnection, day: datetime.date) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_COLS} FROM audit_events "
            "WHERE (created_at AT TIME ZONE 'UTC')::date = :day ORDER BY seq"
        ),
        {"day": day},
    )
    return result.all()


async def record_seal(
    conn: AsyncConnection,
    *,
    seal_id: uuid.UUID,
    day: datetime.date,
    segment_ref: str,
    event_count: int,
    max_seq: int,
    digest: str,
    tip_hash: str,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO audit_seals "
            "(id, day, segment_ref, event_count, max_seq, digest, tip_hash) "
            "VALUES (:id, :day, :segment_ref, :event_count, :max_seq, :digest, :tip_hash) "
            "ON CONFLICT (day) DO NOTHING"
        ),
        {
            "id": seal_id,
            "day": day,
            "segment_ref": segment_ref,
            "event_count": event_count,
            "max_seq": max_seq,
            "digest": digest,
            "tip_hash": tip_hash,
        },
    )


async def sealed_tip_below(conn: AsyncConnection, seq: int) -> str | None:
    """The archived chain tip (tip_hash of the seal with the highest max_seq that
    is < ``seq``), or None if nothing below ``seq`` is sealed. verify_chain anchors
    the first online row to this."""
    result = await conn.execute(
        text("SELECT tip_hash FROM audit_seals WHERE max_seq < :seq ORDER BY max_seq DESC LIMIT 1"),
        {"seq": seq},
    )
    return result.scalar_one_or_none()


async def delete_sealed_before(conn: AsyncConnection, cutoff: datetime.datetime) -> int:
    """Prune the online ledger to the sealed archive (SEC-093). Deletes the
    CONTIGUOUS prefix of rows ``seq <= safe_max``, where ``safe_max`` is the
    highest seal max_seq such that EVERY row up to it is both aged (created_at <
    cutoff) AND archived (seq <= its day's seal max_seq). This guarantees:
      * no unarchived row is ever deleted (a boundary-straddling row whose seq
        exceeds its day's sealed max_seq is preserved, never silently lost);
      * the delete is always a clean prefix ending at a seal tip, so the surviving
        chain still verifies and verify_chain's anchor (the first online row's
        prev_hash == that seal's tip_hash) holds.
    Sets the ``titan.audit_retention`` GUC so the append-only trigger permits this
    one sanctioned archival delete; an ordinary DELETE still fails (SEC-091)."""
    await conn.execute(text("SET LOCAL titan.audit_retention = 'on'"))
    result = await conn.execute(
        text(
            "DELETE FROM audit_events WHERE seq <= COALESCE(("
            "  SELECT MAX(s.max_seq) FROM audit_seals s WHERE NOT EXISTS ("
            "    SELECT 1 FROM audit_events e WHERE e.seq <= s.max_seq AND ("
            "      e.created_at >= :cutoff "
            "      OR NOT EXISTS (SELECT 1 FROM audit_seals s2 "
            "        WHERE s2.day = (e.created_at AT TIME ZONE 'UTC')::date "
            "        AND e.seq <= s2.max_seq)"
            "    )"
            "  )"
            "), 0)"
        ),
        {"cutoff": cutoff},
    )
    return result.rowcount or 0
