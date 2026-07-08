"""The Orchestrator watchdog — a plain asyncio service, never an LLM agent
(Vol 2 §4.2 TITAN-AGT-009, §6.3, §5.4 TITAN-AGT-014, §10 R6).

Four behaviors, all built on the EXISTING delivery + escalation machinery (one
delivery mechanism, one escalation ladder — no parallel channels):

1. **Stalled-consumer restart** — each supervised agent inbox runs one
   :class:`~titan.runtime.worker.RuntimeWorker` drive loop. A dead loop (crashed
   or killed) is detected the moment its task is done; a hung one when its
   heartbeat exceeds ``stalled_after_ms``. The replacement (a fresh consumer
   name) immediately takes over the dead consumer's pending stream entries and
   resumes. Turn emissions carry deterministic ids (Phase 29), so reprocessing a
   reclaimed message re-sends NOTHING — zero duplicate sends across a kill.
2. **Unacked monitor** — the drive loop's reclaim pass re-enters idle pending
   entries via the existing Phase-27 hook; for each reclaimed ``requires_ack``
   message older than its TYPE-SPECIFIC timeout (§6.3), the watchdog escalates
   to the recipient's supervisor through the Phase-30/31 ladder shape.
3. **Escalation deduplication** — escalations are keyed on the dispute
   (``conflict_id`` for ConflictResolution, else the thread) with a
   DETERMINISTIC message id, so repeated flare-ups of one dispute produce
   exactly one escalation, never a storm at the CEO/human tier (§10 R6).
4. **Discussion-deadline enforcement** — an ArchitectureDiscussion thread past
   its ``decision_deadline`` with no DecisionLog is escalated to the moderator's
   supervisor and auto-closed with a DecisionLog recording the expiry
   (TITAN-AGT-014). Escalate-then-close ordering makes a crash between the two
   safe: the still-open thread is re-swept and the escalation dedups.

Every timeout/threshold is read fresh from ``watchdog_config`` each time it is
needed — a change takes effect on the next sweep, no redeploy (TITAN-AGT-020).
"""

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.events.service import QueueBus
from titan.messaging.service import (
    AgentMessage,
    ack_timeout_ms,
    expired_discussion_threads,
    message_exists,
    register_watchdog,
    send_agent_message,
)
from titan.runtime import metrics
from titan.runtime import repository as repo
from titan.runtime.recovery import LadderConfig
from titan.runtime.turn import AgentRuntime
from titan.runtime.worker import RuntimeWorker

logger = structlog.get_logger("titan.runtime.watchdog")

# Namespace for the watchdog's deterministic message ids: one escalation per
# dispute key, one close per discussion thread — idempotent across sweeps,
# restarts, and racing watchdog instances.
WATCHDOG_NS = uuid.UUID("3d1f7a9e-8c2b-5e4d-a6f0-1b9c3e7d5a2c")

# §5.4: discussion threads run with a named moderator — the Architect.
DISCUSSION_MODERATOR = "architect"

# The role the watchdog speaks as: it is the Orchestrator's runtime-level,
# non-LLM component (§4.2) — its messages are Orchestrator messages.
WATCHDOG_ACTOR = "orchestrator"


@dataclass(frozen=True)
class WatchdogConfig:
    """The watchdog's live thresholds (from ``watchdog_config``, TITAN-AGT-020).

    ``stalled_after_ms`` deliberately exceeds the worst legitimate turn (the
    900 s run soft-timeout + slack): a DEAD consumer is caught immediately via
    its task state, so the heartbeat only needs to catch true hangs."""

    stalled_after_ms: int = 960_000
    sweep_interval_ms: int = 5_000
    reclaim_min_idle_ms: int = 960_000
    ack_timeout_overrides: dict[str, int] = field(default_factory=dict)

    def effective_ack_timeout_ms(self, message_type: str) -> int:
        """The type-specific requires_ack timeout (§6.3): the live override if
        set, else the code default (bus ACK_TIMEOUTS_MS)."""
        override = self.ack_timeout_overrides.get(message_type)
        return int(override) if override is not None else ack_timeout_ms(message_type)


async def load_watchdog_config(
    conn: AsyncConnection, project_id: uuid.UUID | None = None
) -> WatchdogConfig:
    """The effective config: the project row if set, else the org default row,
    else code defaults. Read fresh on every call — never cached — so a change
    takes effect on the next sweep with no redeploy (TITAN-AGT-020)."""
    row = None
    if project_id is not None:
        row = await repo.get_config_for_project(conn, project_id)
    if row is None:
        row = await repo.get_global_config(conn)
    if row is None:
        return WatchdogConfig()
    overrides = row.ack_timeout_overrides
    if isinstance(overrides, str):  # asyncpg may hand jsonb back as text
        overrides = json.loads(overrides)
    return WatchdogConfig(
        stalled_after_ms=row.stalled_after_ms,
        sweep_interval_ms=row.sweep_interval_ms,
        reclaim_min_idle_ms=row.reclaim_min_idle_ms,
        ack_timeout_overrides={str(k): int(v) for k, v in overrides.items()},
    )


async def set_watchdog_config(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID | None,
    stalled_after_ms: int,
    sweep_interval_ms: int,
    reclaim_min_idle_ms: int,
    ack_timeout_overrides: dict[str, int] | None = None,
) -> WatchdogConfig:
    """Set the watchdog thresholds (org default when ``project_id`` is None).
    Takes effect on the next sweep (TITAN-AGT-020)."""
    if min(stalled_after_ms, sweep_interval_ms, reclaim_min_idle_ms) < 1:
        raise ValueError("watchdog thresholds must be >= 1 ms")
    # Every per-type ack override must be a positive int: a zero/negative value
    # would make every reclaim escalate instantly, a non-int would crash the
    # hot-path config load — reject at the write boundary.
    for message_type, ms in (ack_timeout_overrides or {}).items():
        if not isinstance(ms, int) or isinstance(ms, bool) or ms < 1:
            raise ValueError(f"ack_timeout_overrides[{message_type!r}] must be an int >= 1 ms")
    row = await repo.upsert_config(
        conn,
        config_id=uuid7(),
        project_id=project_id,
        stalled_after_ms=stalled_after_ms,
        sweep_interval_ms=sweep_interval_ms,
        reclaim_min_idle_ms=reclaim_min_idle_ms,
        ack_timeout_overrides=json.dumps(ack_timeout_overrides or {}),
    )
    overrides = row.ack_timeout_overrides
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    return WatchdogConfig(
        stalled_after_ms=row.stalled_after_ms,
        sweep_interval_ms=row.sweep_interval_ms,
        reclaim_min_idle_ms=row.reclaim_min_idle_ms,
        ack_timeout_overrides={str(k): int(v) for k, v in overrides.items()},
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def dedup_key(message: AgentMessage) -> str:
    """The §10 R6 dispute key: ``conflict_id`` for a ConflictResolution, else the
    thread (falling back to the correlation id, which seeds new threads)."""
    if message.type == "ConflictResolution":
        conflict_id = message.payload.get("conflict_id")
        if conflict_id:
            return f"conflict:{conflict_id}"
    return f"thread:{message.thread_id or message.correlation_id}"


def escalation_message_id(project_id: uuid.UUID, cause: str, target: str, key: str) -> uuid.UUID:
    """The deterministic id of the ONE escalation for (dispute ``key``, ``cause``,
    ``target``). ``cause`` and ``target`` are part of the identity so a repeated
    flare-up of the SAME dispute→same supervisor dedups to one message (§10 R6),
    while a DIFFERENT cause (an unacked-message escalation vs. a discussion-deadline
    escalation on the same thread) or a ladder CLIMB (a new target) is a distinct,
    legitimately-new escalation — never silently suppressed by the first."""
    return uuid.uuid5(WATCHDOG_NS, f"{project_id}:escalation:{cause}:{target}:{key}")


@dataclass
class _Supervised:
    """One supervised inbox consumer: the live worker, its drive task, and the
    heartbeat the sweep checks."""

    project_id: uuid.UUID
    agent_id: str
    worker: RuntimeWorker
    task: asyncio.Task[None]
    last_beat: float
    generation: int = 1


@dataclass
class SweepReport:
    """What one watchdog sweep did — the test/observability surface."""

    restarted: list[str] = field(default_factory=list)  # "<project>:<agent>" replaced
    escalated: list[uuid.UUID] = field(default_factory=list)  # escalation message ids
    discussions_closed: list[uuid.UUID] = field(default_factory=list)  # thread ids


class Watchdog:
    """The non-LLM Orchestrator watchdog (TITAN-AGT-009).

    One instance supervises any number of (project, agent) inbox consumers and
    runs the deadline/dedup sweeps. All watchdog messages carry deterministic
    ids, so overlapping sweeps (or a second watchdog instance racing this one)
    send nothing twice — the loser of the insert race folds silently."""

    def __init__(self, runtime: AgentRuntime, bus: QueueBus, *, poll_interval_s: float = 0.5):
        self._runtime = runtime
        self._bus = bus
        self._poll_interval_s = poll_interval_s
        self._supervised: dict[tuple[uuid.UUID, str], _Supervised] = {}
        self._registered = False

    # -- wiring --------------------------------------------------------------

    def register(self) -> None:
        """Register the unacked-monitor hook on the Phase-27 seam (idempotent for
        this instance): every requires_ack message reclaimed past its idle
        threshold flows through :meth:`_on_unacked`."""
        if not self._registered:
            register_watchdog(self._on_unacked)
            self._registered = True

    # -- consumer supervision (behavior 1) ------------------------------------

    def supervise(self, project_id: uuid.UUID, agent_id: str) -> None:
        """Ensure a live consumer drives ``agent_id``'s inbox for ``project_id``."""
        key = (project_id, agent_id)
        if key in self._supervised and not self._supervised[key].task.done():
            return
        self._start(key, generation=1)

    def kill(self, project_id: uuid.UUID, agent_id: str) -> None:
        """Kill the supervised consumer (test/ops helper — simulates a crash).
        The next sweep detects the dead task and starts a replacement."""
        sup = self._supervised.get((project_id, agent_id))
        if sup is not None:
            sup.task.cancel()

    def _start(self, key: tuple[uuid.UUID, str], *, generation: int) -> None:
        project_id, agent_id = key
        worker = RuntimeWorker(
            self._runtime,
            self._bus,
            project_id,
            agent_id,
            consumer=f"watchdog-{agent_id}-g{generation}",
        )
        sup = _Supervised(
            project_id=project_id,
            agent_id=agent_id,
            worker=worker,
            task=asyncio.get_running_loop().create_task(self._drive(key)),
            last_beat=time.monotonic(),
            generation=generation,
        )
        self._supervised[key] = sup

    async def _drive(self, key: tuple[uuid.UUID, str]) -> None:
        """The consumer loop for one supervised inbox: reclaim abandoned pending
        entries (firing the unacked hooks), process ONE new message, beat. Reads
        the reclaim threshold fresh each iteration (TITAN-AGT-020).

        The heartbeat is refreshed at the TOP of each iteration — the loop cycling
        is the liveness signal. Only a consumer wedged INSIDE one step (a hung
        turn) lets the beat go stale, and a single turn is bounded well under
        ``stalled_after_ms``; a healthy consumer chewing a long backlog beats once
        per turn (``max_turns=1``), so it is never falsely restarted mid-backlog."""
        while True:
            sup = self._supervised.get(key)
            if sup is None or sup.task is not asyncio.current_task():
                # Replaced (or unsupervised) — this task is an orphan; stop it so
                # two loops never co-drive one worker.
                return
            sup.last_beat = time.monotonic()
            async with self._runtime.engine.connect() as conn:
                cfg = await load_watchdog_config(conn, sup.project_id)
            await sup.worker.process_reclaimed(min_idle_ms=cfg.reclaim_min_idle_ms)
            await sup.worker.process_available(max_turns=1, block_ms=50)
            await asyncio.sleep(self._poll_interval_s)

    async def _restart_stalled(self, report: SweepReport) -> None:
        """Behavior 1: replace dead (task done) or hung (heartbeat stale)
        consumers. Each consumer's stall threshold is read from ITS project's
        config (TITAN-AGT-020). The replacement seizes the dead consumer's pending
        entries immediately (idle 0 — it IS dead), so an in-flight unacked message
        is reprocessed at once; deterministic emission ids make that a no-op for
        anything the dead consumer already committed (zero duplicate sends)."""
        for key, sup in list(self._supervised.items()):
            # Dead = the drive task finished (crashed) OR has a cancellation in
            # flight that has not yet unwound (a just-killed consumer). Either way
            # it is no longer consuming, so replace it now rather than waiting out
            # the heartbeat.
            is_dead = sup.task.done() or sup.task.cancelling() > 0
            stale_ms = (time.monotonic() - sup.last_beat) * 1000.0
            if not is_dead:
                async with self._runtime.engine.connect() as conn:
                    cfg = await load_watchdog_config(conn, sup.project_id)
                if stale_ms <= cfg.stalled_after_ms:
                    continue
            sup.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await sup.task
                except Exception:  # noqa: BLE001 — a crashed loop is why we're here
                    logger.warning(
                        "watchdog.consumer_crashed", agent_id=sup.agent_id, exc_info=True
                    )
            generation = sup.generation + 1
            self._start(key, generation=generation)
            replacement = self._supervised[key]
            # Take over the dead consumer's pending set NOW rather than waiting
            # out the idle threshold — it is confirmed dead, not slow.
            await replacement.worker.process_reclaimed(min_idle_ms=0)
            metrics.observe_watchdog_restart(f"agent.{sup.agent_id}")
            report.restarted.append(f"{sup.project_id}:{sup.agent_id}")
            logger.warning(
                "watchdog.consumer_restarted",
                agent_id=sup.agent_id,
                project_id=str(sup.project_id),
                generation=generation,
                was_dead=is_dead,
                stale_ms=int(stale_ms),
            )

    # -- unacked monitor + dedup escalation (behaviors 2 + 3) -----------------

    async def _on_unacked(self, message: AgentMessage) -> None:
        """The Phase-27 hook: a reclaimed requires_ack message. Escalate to the
        recipient's supervisor once it is older than its TYPE-SPECIFIC timeout
        (§6.3) — deduplicated per dispute (§10 R6)."""
        async with self._runtime.engine.connect() as conn:
            cfg = await load_watchdog_config(conn, message.project_id)
        age_ms = (_utcnow() - message.created_at).total_seconds() * 1000.0
        if age_ms < cfg.effective_ack_timeout_ms(message.type):
            return  # reclaimed for reprocessing, but not yet past its ack timeout
        target = self._escalation_target(message.to_agent)
        if target is None:
            # A human-targeted (or unknown-role) message has no supervisor tier
            # above it — it is surfaced on the dashboard, never storm-escalated.
            logger.info(
                "watchdog.unacked_no_target",
                to_agent=message.to_agent,
                message_type=message.type,
            )
            return
        sent = await self._escalate_once(
            message.project_id,
            dedup_key(message),
            target,
            reason=(
                f"requires_ack {message.type} to {message.to_agent} went unacknowledged "
                f"past its timeout; the watchdog reclaimed it"
            ),
            thread_id=message.thread_id or message.correlation_id,
            cause="unacked",
        )
        if sent is not None:
            logger.warning(
                "watchdog.unacked_escalated",
                message_type=message.type,
                to_agent=message.to_agent,
                target=target,
            )

    def _escalation_target(self, agent_id: str) -> str | None:
        """Where an escalation about ``agent_id`` goes: its supervisor per the
        roster (or the human for a no-supervisor role). None for non-roster
        recipients (e.g. ``human``)."""
        definition = self._runtime.registry.get(agent_id)
        if definition is None:
            return None
        return LadderConfig.from_definition(definition).escalation_target()

    async def _escalate_once(
        self,
        project_id: uuid.UUID,
        key: str,
        target: str,
        *,
        reason: str,
        thread_id: uuid.UUID | None,
        cause: str,
    ) -> uuid.UUID | None:
        """Send ONE escalation per (dispute key, cause, target) (§10 R6): the
        deterministic id + the ``message_exists`` guard + the primary-key fold make
        repeated flare-ups, overlapping sweeps, and racing watchdog instances all
        collapse onto a single persisted escalation. Returns the message id if THIS
        call sent it, else None."""
        message_id = escalation_message_id(project_id, cause, target, key)
        try:
            async with self._runtime.engine.begin() as conn:
                if await message_exists(conn, message_id):
                    return None
                escalation = AgentMessage(
                    message_id=message_id,
                    type="ProgressReport",
                    project_id=project_id,
                    thread_id=thread_id,
                    from_agent=WATCHDOG_ACTOR,
                    to_agent=target,
                    priority="high",
                    requires_ack=True,
                    payload={
                        "status": "blocked",
                        "percent_complete": 0,
                        "summary": reason,
                        "blockers": [{"description": reason, "needs": target}],
                    },
                )
                await send_agent_message(conn, escalation, actor=WATCHDOG_ACTOR)
        except IntegrityError:
            return None  # lost the race to a concurrent sweep — already escalated
        metrics.observe_watchdog_escalation(cause)
        return message_id

    # -- discussion-deadline enforcement (behavior 4) --------------------------

    async def _enforce_discussion_deadlines(self, report: SweepReport, *, now: datetime) -> None:
        """§5.4/TITAN-AGT-014: a discussion past its decision deadline with no
        DecisionLog escalates to the moderator's supervisor AND is auto-closed
        with a DecisionLog recording the expiry. Escalate-then-close: a crash
        between the two leaves the thread open for the next sweep, and the
        escalation dedups — nothing is lost, nothing repeats."""
        async with self._runtime.engine.connect() as conn:
            expired = await expired_discussion_threads(conn, now=now)
        target = self._escalation_target(DISCUSSION_MODERATOR)
        for item in expired:
            if target is not None:
                sent = await self._escalate_once(
                    item.project_id,
                    f"thread:{item.thread_id}",
                    target,
                    reason=(
                        "ArchitectureDiscussion thread passed its decision deadline "
                        "without a DecisionLog (TITAN-AGT-014)"
                    ),
                    thread_id=item.thread_id,
                    cause="discussion",
                )
                if sent is not None:
                    report.escalated.append(sent)
            closed = await self._close_discussion(item.project_id, item.thread_id)
            if closed:
                report.discussions_closed.append(item.thread_id)
                metrics.observe_discussion_closed()
                logger.warning(
                    "watchdog.discussion_closed",
                    project_id=str(item.project_id),
                    thread_id=str(item.thread_id),
                )

    async def _close_discussion(self, project_id: uuid.UUID, thread_id: uuid.UUID) -> bool:
        """Write the auto-close DecisionLog into the thread (idempotent): it
        records the EXPIRY — the watchdog never invents a technical decision;
        the open question travels up via the escalation."""
        message_id = uuid.uuid5(WATCHDOG_NS, f"{project_id}:discussion-close:{thread_id}")
        try:
            async with self._runtime.engine.begin() as conn:
                if await message_exists(conn, message_id):
                    return False
                close = AgentMessage(
                    message_id=message_id,
                    type="DecisionLog",
                    project_id=project_id,
                    thread_id=thread_id,
                    from_agent=WATCHDOG_ACTOR,
                    to_agent=DISCUSSION_MODERATOR,
                    priority="high",
                    payload={
                        "decision": (
                            "Auto-closed: the discussion reached its decision deadline "
                            "without a recorded DecisionLog."
                        ),
                        "context": (
                            "Deadline enforcement by the Orchestrator watchdog "
                            "(§5.4, TITAN-AGT-014)."
                        ),
                        "alternatives": [],
                        "trade_offs": (
                            "The open question was escalated to the moderator's supervisor."
                        ),
                        "authority": "watchdog",
                        "reversible": True,
                    },
                )
                await send_agent_message(conn, close, actor=WATCHDOG_ACTOR)
        except IntegrityError:
            return False
        return True

    # -- the sweep + service loop ---------------------------------------------

    async def sweep(self, *, now: datetime | None = None) -> SweepReport:
        """One bounded watchdog pass: restart dead/hung consumers and enforce
        discussion deadlines. Config is read fresh here, so threshold changes
        apply from this sweep on (TITAN-AGT-020). Unacked monitoring rides the
        drive loops' reclaim pass continuously between sweeps."""
        report = SweepReport()
        await self._restart_stalled(report)
        await self._enforce_discussion_deadlines(report, now=now or _utcnow())
        return report

    async def run_forever(self, *, stop: asyncio.Event) -> None:
        """The background service loop (the FastAPI lifespan schedules this):
        sweep, then sleep the LIVE sweep interval."""
        while not stop.is_set():
            cfg = WatchdogConfig()  # fall back to code defaults if the DB blips
            try:
                await self.sweep()
                async with self._runtime.engine.connect() as conn:
                    cfg = await load_watchdog_config(conn)
            except Exception:  # noqa: BLE001 — one bad sweep/read must not kill the service
                logger.error("watchdog.sweep_failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=cfg.sweep_interval_ms / 1000.0)

    async def stop(self) -> None:
        """Cancel every supervised consumer and forget them (shutdown path)."""
        tasks = [sup.task for sup in self._supervised.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._supervised.clear()


def build_watchdog(
    runtime: AgentRuntime, bus: QueueBus, *, poll_interval_s: float = 0.5
) -> Watchdog:
    """Construct the watchdog over an existing runtime + queue bus. The caller
    invokes :meth:`Watchdog.register` to attach the unacked hook and
    :meth:`Watchdog.supervise` per (project, agent) inbox."""
    return Watchdog(runtime, bus, poll_interval_s=poll_interval_s)


__all__ = [
    "DISCUSSION_MODERATOR",
    "WATCHDOG_NS",
    "SweepReport",
    "Watchdog",
    "WatchdogConfig",
    "build_watchdog",
    "dedup_key",
    "escalation_message_id",
    "load_watchdog_config",
    "set_watchdog_config",
]
