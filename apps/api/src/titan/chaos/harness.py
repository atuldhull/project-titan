"""The chaos harness (TITAN-FND-052 / TITAN-FND-007, DoD items 1 & 2).

Seeds a mini project — durable message history plus in-flight domain events on
the transactional outbox — then SIMULATES each service failure IN-PROCESS and
drives recovery through the *real* production components (the outbox drain path,
:class:`RedisStreamBus`, the domain consumer + inbox ledger, :class:`ProjectBuffer`).
The simulations model the failure POINT (a crash between two operations, a lost
queue, an interrupted transaction) — not process death, WAL recovery, or network
partition, which are live-Compose concerns. Each scenario returns a
:class:`ChaosResult` whose fields are durable, checkable facts: acknowledged loss,
redelivery count, duplicate side effects, and recovery-work latency.

The five failures map to distinct failure points, not five spellings of one:

* ``orchestrator`` — crashes AFTER committing state to PostgreSQL but BEFORE the
  outbox drainer publishes; on restart the drainer re-drains and the project
  resumes (TITAN-FND-007 verbatim).
* ``worker``       — crashes after committing its side effect but before the ack;
  the replacement reclaims the idle-pending entry and REPROCESSES it, and the
  reprocess is verified to produce no duplicate (at-least-once + idempotent).
* ``postgres``     — a transaction is interrupted before commit; it rolls back
  atomically (no partial state, no orphaned event) and a committed event is
  durable in a later transaction.
* ``backend_api``  — the reconnect resume mechanism: the per-project replay buffer
  serves the post-cursor gap in seq order (the real /ws reconnect is proven in the
  realtime endpoint tests).
* ``redis``        — total queue data loss; the outbox (system of record) replays
  every unacknowledged event so no committed work is lost.
"""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.chaos.model import ChaosResult, ChaosRunReport, KillTarget
from titan.chaos.reconcile import reconcile_domain_ledger
from titan.db import uuid7
from titan.events.consumers import DOMAIN_GROUP, handle_domain_delivery, record_inbox
from titan.events.outbox import write_event
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.realtime.buffer import RESYNC, ProjectBuffer

_STREAM = STREAM_EVENTS_DOMAIN


class _SimulatedCrash(Exception):
    """Raised inside a transaction to model PostgreSQL dying before commit."""


@dataclass
class SeededProject:
    """A mini project's durable footprint before a kill."""

    project_id: uuid.UUID
    creator_id: uuid.UUID
    event_ids: list[str] = field(default_factory=list)
    message_count: int = 0


class ChaosHarness:
    """Drives the five service-kill scenarios against one engine + Redis bus.

    ``clock`` defaults to :func:`time.monotonic` and is injectable so a test can
    make resume timing deterministic; only the ``≤ 60 s`` budget is asserted."""

    def __init__(
        self,
        engine: AsyncEngine,
        bus: RedisStreamBus,
        redis: object,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.engine = engine
        self.bus = bus
        self._redis = redis
        self.clock = clock or time.monotonic

    # -- seeding ------------------------------------------------------------

    async def seed(self, *, messages: int = 3, events: int = 4) -> SeededProject:
        """Create a project with ``messages`` history rows and ``events`` domain
        events committed to the outbox (not yet drained)."""
        pid = uuid7()
        uid = uuid7()
        event_ids: list[str] = []
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :e, 'chaos', 'local', :s)"
                ),
                {"id": uid, "e": f"chaos-{uid.hex[:12]}@titan.local", "s": str(uid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, created_by) "
                    "VALUES (:id, :n, 'Chaos', 'b', :u)"
                ),
                {"id": pid, "n": f"chaos-{pid.hex[:12]}", "u": uid},
            )
            for i in range(messages):
                await conn.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, project_id, thread_id, kind, sender_user_id, body) "
                        "VALUES (:id, :pid, :tid, 'progress_report', :uid, CAST(:body AS jsonb))"
                    ),
                    {
                        "id": uuid7(),
                        "pid": pid,
                        "tid": pid,
                        "uid": uid,
                        "body": json.dumps({"seq": i, "text": f"history {i}"}),
                    },
                )
            for i in range(events):
                eid = await write_event(
                    conn,
                    _STREAM,
                    {"project_id": str(pid), "kind": "task.ready", "seq": i},
                    actor="orchestrator",
                )
                event_ids.append(str(eid))
        return SeededProject(
            project_id=pid, creator_id=uid, event_ids=event_ids, message_count=messages
        )

    # -- shared plumbing ----------------------------------------------------

    async def _publish_events(self, event_ids: list[str]) -> int:
        """Drain THIS scenario's committed outbox events to the stream (publish +
        stamp ``published_at``), exactly as :class:`OutboxDrainer` does but scoped
        by id. Scoping is a test-isolation necessity: the outbox is shared, and a
        global drain would publish unrelated rows into the scenario's stream."""
        async with self.engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, topic, payload FROM outbox "
                        "WHERE id = ANY(CAST(:ids AS uuid[])) AND published_at IS NULL "
                        "ORDER BY created_at"
                    ),
                    {"ids": event_ids},
                )
            ).all()
            for row in rows:
                wrapper = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
                await self.bus.publish(
                    row.topic,
                    wrapper.get("data", {}),
                    actor=wrapper.get("actor", "system"),
                    event_id=str(row.id),
                )
                await conn.execute(
                    text("UPDATE outbox SET published_at = now() WHERE id = :id"),
                    {"id": row.id},
                )
        return len(rows)

    async def _consume_and_ack(self, consumer: str, *, limit: int = 100) -> int:
        """Consume new domain deliveries and handle each (idempotent record + ack)."""
        handled = 0
        while handled < limit:
            deliveries = await self.bus.consume(
                _STREAM, DOMAIN_GROUP, consumer, count=50, block_ms=10
            )
            if not deliveries:
                return handled
            for delivery in deliveries:
                await handle_domain_delivery(self.engine, self.bus, delivery)
                handled += 1
        return handled

    async def _inbox_event_ids(self, project_event_ids: list[str]) -> set[str]:
        wanted = tuple(project_event_ids)
        if not wanted:
            return set()
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT event_id FROM event_inbox WHERE consumer_group = :g "
                    "AND event_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"g": DOMAIN_GROUP, "ids": list(wanted)},
            )
        return {str(r.event_id) for r in rows}

    async def _message_count(self, pid: uuid.UUID) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM messages WHERE project_id = :pid"), {"pid": pid}
            )
        return int(result.scalar_one())

    def _loss_and_dupes(self, seed: SeededProject, acked: set[str]) -> tuple[int, int]:
        loss = len(set(seed.event_ids) - acked)
        # Used only by scenarios that never REPROCESS an already-processed message
        # (orchestrator = first delivery on restart; redis = replay of never-
        # processed events), so no duplicate side effect is possible — duplicates
        # are genuinely EXERCISED and measured in kill_worker instead.
        return loss, 0

    # -- scenario: kill orchestrator (TITAN-FND-007) ------------------------

    async def kill_orchestrator(self, seed: SeededProject) -> ChaosResult:
        """Crash between commit and publish: events are durable in the outbox but
        were never drained. On restart the drainer publishes and the consumer
        processes them — full message history intact, project resumes."""
        await self.bus.ensure_group(_STREAM, DOMAIN_GROUP)
        async with self.engine.connect() as conn:
            unpublished = int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM outbox WHERE topic = :t "
                            "AND published_at IS NULL AND id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"t": _STREAM, "ids": seed.event_ids},
                    )
                ).scalar_one()
            )
        t0 = self.clock()
        await self._publish_events(seed.event_ids)
        await self._consume_and_ack("orchestrator-restarted")
        resume = self.clock() - t0
        acked = await self._inbox_event_ids(seed.event_ids)
        loss, dupes = self._loss_and_dupes(seed, acked)
        return ChaosResult(
            target=KillTarget.ORCHESTRATOR,
            acknowledged_loss=loss,
            redelivered=len(seed.event_ids),
            duplicated=dupes,
            resume_seconds=resume,
            history_intact=await self._message_count(seed.project_id) == seed.message_count,
            detail=f"{unpublished} committed-but-unpublished events re-drained on restart",
        )

    # -- scenario: kill worker (at-least-once redelivery) -------------------

    async def kill_worker(self, seed: SeededProject) -> ChaosResult:
        """The at-least-once + idempotent hazard, exercised for real. The worker
        COMMITS its side effect (the ``event_inbox`` row) and then crashes BEFORE
        the ack, so the message is redelivered to a replacement that has already
        been processed once. The replacement's reprocess must be recognized as a
        replay (no second side effect) — that is the property idempotency buys, and
        it is measured here (``duplicated``), not assumed."""
        await self.bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await self._publish_events(seed.event_ids)
        # Worker A consumes and commits the side effect but crashes pre-ack: the
        # inbox row is durable AND the message stays in the PEL for redelivery.
        dead = await self.bus.consume(_STREAM, DOMAIN_GROUP, "worker-dead", count=100, block_ms=10)
        for delivery in dead:
            await record_inbox(self.engine, DOMAIN_GROUP, delivery)  # committed, never acked
        processed_once = len(await self._inbox_event_ids(seed.event_ids))
        pending_before = await self.bus.pending_depth(_STREAM, DOMAIN_GROUP)
        # Worker B (the replacement) reclaims the still-pending messages and
        # REPROCESSES them: record_inbox must dedup (insert nothing), then ack.
        t0 = self.clock()
        reclaimed = await self.bus.reap(_STREAM, DOMAIN_GROUP, "worker-restarted", min_idle_ms=0)
        duplicated = 0
        for delivery in reclaimed:
            inserted = await record_inbox(self.engine, DOMAIN_GROUP, delivery)
            if inserted:
                duplicated += 1  # a fresh side effect on reprocess = a real duplicate
            await self.bus.ack(_STREAM, DOMAIN_GROUP, delivery)
        resume = self.clock() - t0
        acked = await self._inbox_event_ids(seed.event_ids)
        loss = len(set(seed.event_ids) - acked)
        return ChaosResult(
            target=KillTarget.WORKER,
            acknowledged_loss=loss,
            redelivered=len(reclaimed),
            duplicated=duplicated,
            resume_seconds=resume,
            history_intact=(
                processed_once == len(seed.event_ids)
                and await self._message_count(seed.project_id) == seed.message_count
            ),
            detail=(
                f"{len(dead)} processed then crashed pre-ack, {pending_before} pending, "
                f"{len(reclaimed)} reclaimed + reprocessed with {duplicated} duplicates"
            ),
        )

    # -- scenario: kill postgres (atomicity + durability) -------------------

    async def kill_postgres(self, seed: SeededProject) -> ChaosResult:
        """A transaction interrupted before commit rolls back ATOMICALLY — a torn
        write (message + event in one unit) leaves NO partial state and NO orphaned
        event — and a separately-committed event is DURABLE (queryable in a later
        transaction). This proves the transactional guarantee PostgreSQL-as-system-
        of-record rests on; it does not (and cannot, in-process) reproduce a real
        `postgres` process death with WAL recovery — that is a live-Compose test."""
        before = await self._message_count(seed.project_id)
        # Atomicity: the whole unit rolls back when the "crash" fires pre-commit.
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, project_id, thread_id, kind, sender_user_id, body) "
                        "VALUES (:id, :pid, :tid, 'progress_report', :uid, CAST(:b AS jsonb))"
                    ),
                    {
                        "id": uuid7(),
                        "pid": seed.project_id,
                        "tid": seed.project_id,
                        "uid": seed.creator_id,
                        "b": json.dumps({"text": "uncommitted"}),
                    },
                )
                await write_event(
                    conn, _STREAM, {"project_id": str(seed.project_id), "kind": "torn"}, actor="pg"
                )
                raise _SimulatedCrash
        except _SimulatedCrash:
            pass
        after = await self._message_count(seed.project_id)
        # Durability: a committed event is queryable in a later, independent
        # transaction (committed data persists — the property recovery relies on).
        # NOTE: we do NOT dispose the caller's engine here — disposing a shared,
        # session-scoped pool would be a surprising side effect on other callers.
        t0 = self.clock()
        async with self.engine.begin() as conn:
            committed = await write_event(
                conn, _STREAM, {"project_id": str(seed.project_id), "kind": "durable"}, actor="pg"
            )
        async with self.engine.connect() as conn:
            survived = (
                await conn.execute(
                    text("SELECT count(*) FROM outbox WHERE id = :id"), {"id": committed}
                )
            ).scalar_one() == 1
        resume = self.clock() - t0
        torn_leaked = after != before
        return ChaosResult(
            target=KillTarget.POSTGRES,
            acknowledged_loss=(0 if survived else 1),
            redelivered=0,
            duplicated=(1 if torn_leaked else 0),
            resume_seconds=resume,
            history_intact=(after == before and survived),
            detail=(
                f"torn write rolled back ({before}=={after}), "
                f"committed event durable in a later transaction={survived}"
            ),
        )

    # -- scenario: kill backend api (reconnect resume mechanism) ------------

    async def kill_backend_api(self, seed: SeededProject) -> ChaosResult:
        """Exercise the mechanism a reconnecting WebSocket client resumes from: the
        per-project replay buffer (:class:`ProjectBuffer`) serves the gap after a
        client's last cursor, in seq order, with no missed frames and no resync.
        This is the buffer-level resume path — the real /ws reconnect over the live
        transport is proven separately in the realtime endpoint tests; here we prove
        the buffer returns exactly the right frames in order."""
        buffer = ProjectBuffer(self._redis)  # type: ignore[arg-type]
        pid = str(seed.project_id)
        frames: list[dict[str, object]] = []
        for i in range(6):
            seq = await buffer.next_project_seq(pid)
            frame = {"seq": seq, "topic": "tasks", "event": "task.updated", "payload": {"i": i}}
            await buffer.append(pid, json.dumps(frame))
            frames.append(frame)
        # A client whose cursor is at frame 3 reconnects; frames 4-6 are the gap.
        last_seq = int(frames[2]["seq"])  # type: ignore[call-overload]
        t0 = self.clock()
        replayed = await buffer.replay(pid, last_seq)
        resume = self.clock() - t0
        if replayed is RESYNC or not isinstance(replayed, list):
            return ChaosResult(
                target=KillTarget.BACKEND_API,
                acknowledged_loss=0,
                redelivered=0,
                resume_seconds=resume,
                history_intact=False,
                detail="reconnect fell out of the replay window (resync required)",
            )
        seqs = [f["seq"] for f in replayed]
        expected = [f["seq"] for f in frames if int(f["seq"]) > last_seq]  # type: ignore[call-overload]
        ordered = seqs == sorted(seqs) and seqs == expected
        return ChaosResult(
            target=KillTarget.BACKEND_API,
            acknowledged_loss=0,
            redelivered=len(replayed),
            duplicated=0,
            resume_seconds=resume,
            history_intact=(
                ordered and await self._message_count(seed.project_id) == seed.message_count
            ),
            detail=f"reconnect replayed {len(replayed)} buffered frames in seq order",
        )

    # -- scenario: kill redis (total queue loss, outbox replay) -------------

    async def kill_redis(self, seed: SeededProject) -> ChaosResult:
        """Total Redis data loss: some events were consumed, then the queue is
        wiped entirely. The outbox is the system of record, so every published-
        but-unacknowledged event is replayed from PostgreSQL — zero committed
        loss despite the broker losing its streams, groups, and PEL."""
        await self.bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await self._publish_events(seed.event_ids)  # publish this run's seed events
        # The consumer processed the first two before Redis died.
        first = await self.bus.consume(_STREAM, DOMAIN_GROUP, "pre-crash", count=2, block_ms=10)
        for delivery in first:
            await handle_domain_delivery(self.engine, self.bus, delivery)
        acked_before = len(await self._inbox_event_ids(seed.event_ids))
        # "Redis dies with total data loss" — streams, groups, PEL, seen-ledger.
        await self._redis.flushall()  # type: ignore[attr-defined]
        t0 = self.clock()
        replayed = await self._replay_unconsumed(seed.event_ids)
        await self._consume_and_ack("redis-restarted")
        resume = self.clock() - t0
        acked = await self._inbox_event_ids(seed.event_ids)
        loss, dupes = self._loss_and_dupes(seed, acked)
        return ChaosResult(
            target=KillTarget.REDIS,
            acknowledged_loss=loss,
            redelivered=replayed,
            duplicated=dupes,
            resume_seconds=resume,
            history_intact=await self._message_count(seed.project_id) == seed.message_count,
            detail=(
                f"{acked_before} acked before wipe, {replayed} events replayed from the "
                f"outbox after total queue loss"
            ),
        )

    async def _replay_unconsumed(self, event_ids: list[str]) -> int:
        """Re-publish this project's published outbox events with no acknowledgement
        — the system-of-record recovery after the queue lost the messages. Scoped to
        ``event_ids`` because the outbox is shared (a global scan would resurrect
        unrelated rows)."""
        await self.bus.ensure_group(_STREAM, DOMAIN_GROUP)
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT o.id, o.payload FROM outbox o WHERE o.topic = :t "
                        "AND o.published_at IS NOT NULL "
                        "AND o.id = ANY(CAST(:ids AS uuid[])) AND NOT EXISTS ("
                        "SELECT 1 FROM event_inbox e WHERE e.event_id = o.id "
                        "AND e.consumer_group = :g)"
                    ),
                    {"t": _STREAM, "g": DOMAIN_GROUP, "ids": event_ids},
                )
            ).all()
        for row in rows:
            wrapper = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
            await self.bus.publish(
                _STREAM,
                wrapper.get("data", {}),
                actor=wrapper.get("actor", "system"),
                event_id=str(row.id),
            )
        return len(rows)

    # -- the full sweep -----------------------------------------------------

    async def run_all(self) -> ChaosRunReport:
        """Kill every service in turn (each against its own seeded project), then
        reconcile the whole domain ledger — the post-chaos proof of zero
        acknowledged-task loss (DoD items 1 & 2)."""
        results: list[ChaosResult] = []
        run_event_ids: set[str] = set()
        # postgres/backend_api do not process domain-stream events (they exercise
        # transaction atomicity and WS replay), so they seed none.
        plan = [
            (self.kill_orchestrator, 4),
            (self.kill_worker, 4),
            (self.kill_postgres, 0),
            (self.kill_backend_api, 0),
            (self.kill_redis, 4),
        ]
        for scenario, events in plan:
            seed = await self.seed(events=events)
            run_event_ids.update(seed.event_ids)
            results.append(await scenario(seed))
        # Scope the reconciliation to THIS run's events — the outbox/inbox are
        # shared, so a global scan would fold in unrelated rows.
        reconciliation = await reconcile_domain_ledger(
            self.engine, self.bus, event_ids=run_event_ids
        )
        return ChaosRunReport(results=results, reconciliation=reconciliation)


__all__ = ["ChaosHarness", "SeededProject"]
