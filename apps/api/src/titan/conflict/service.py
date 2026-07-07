"""Public service interface of the `conflict` module (TITAN-BCK-004).

Responsibility: the §7 conflict-resolution state machine, the Security veto that
gates merge/release, the deadlock detection hook + live thresholds, and the
per-agent authority contract (TITAN-AGT-011/017/018/019/020).

Only this interface may be imported by other modules; the state table internals,
the repository, and the ORM models stay private to the package. The conflict flow
is driven entirely by the Phase-27 ``ConflictResolution`` and ``DecisionLog``
message types and the Phase-29 runtime — there is no separate dispute channel. The
state machine, not prompt compliance, decides what is reachable (TITAN-AGT-017):
every transition is guarded against :data:`ALLOWED_TRANSITIONS` and persisted, and
every resolution produces an append-only ``DecisionLog`` (TITAN-AGT-019).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.agents.service import AgentDefinition, AgentRegistry
from titan.conflict import repository as repo
from titan.conflict.authority import (
    AuthorityContract,
    CoSignRequirement,
    contract_for,
    vetoing_agents,
)
from titan.conflict.state import (
    DeadlockThresholds,
    arbiter_for,
    escalate_one_level,
    is_legal_transition,
    is_terminal,
    reopens_deadlocked,
    round_deadlocked,
)
from titan.db import uuid7
from titan.gateway.problems import ProblemException, problem_type
from titan.messaging.service import AgentMessage, DecisionCreate, DecisionOut, create_decision


def _decode_list(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


#: A jsonb array column decoded to a Python list (asyncpg may hand back a str).
JsonbList = Annotated[list[str], BeforeValidator(_decode_list)]


# --- DTOs ------------------------------------------------------------------


class ConflictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    conflict_id: uuid.UUID
    kind: str
    subject: str
    parties: JsonbList
    state: str
    round: int
    arbiter: str | None
    opened_by: str
    deadlock_flagged: bool
    resolution_decision_id: uuid.UUID | None
    created_at: datetime
    version: int


class ConflictTransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conflict_row_id: uuid.UUID
    from_state: str | None
    to_state: str
    trigger: str
    actor: str
    round: int
    note: str | None
    created_at: datetime


class MergeVetoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    artifact_ref: str
    task_id: uuid.UUID | None
    raised_by: str
    veto_message_id: uuid.UUID | None
    reason: str
    status: str
    override_decision_id: uuid.UUID | None
    created_at: datetime
    version: int


class ThresholdsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: uuid.UUID | None
    max_conflict_rounds: int
    max_task_reopens: int


class ResolutionDecision(BaseModel):
    """The DecisionLog an arbiter or the CEO records to resolve a conflict. Maps
    onto a ``decisions`` row; the authority is stamped by the caller function."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    context: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    alternatives: str = "Alternatives were weighed during the dispute."
    consequences: str = "Binding on the parties per §7."
    decided_by_agent_id: uuid.UUID | None = None


@dataclass(frozen=True)
class DeadlockSignal:
    """A rule-5 deadlock the Orchestrator watchdog (Phase 32) should act on. Pure
    detection output — carries what to escalate to and why, no side effect."""

    scope: Literal["conflict_rounds", "task_reopens"]
    subject_id: uuid.UUID  # the conflict row id, or the task id
    current: int
    threshold: int
    escalate_to: str | None  # the next conflict state, or None for a task
    reason: str


DisputeKind = Literal["technical", "scope", "priority", "cross_domain"]


def _problem(status: int, title: str, detail: str) -> ProblemException:
    return ProblemException(
        status=status, title=title, detail=detail, problem_type=problem_type("conflict")
    )


async def _load(conn: AsyncConnection, project_id: uuid.UUID, conflict_id: uuid.UUID) -> Row[Any]:
    row = await repo.get_conflict_by_cid(conn, project_id, conflict_id)
    if row is None:
        raise _problem(404, "Conflict not found", f"No conflict {conflict_id} in this project.")
    return row


def _require_legal(from_state: str, to_state: str) -> None:
    if not is_legal_transition(from_state, to_state):
        raise _problem(
            409,
            "Illegal conflict transition",
            f"{from_state} -> {to_state} is not a permitted §7 move.",
        )


async def _persist_transition(
    conn: AsyncConnection,
    conflict: Row[Any],
    *,
    to_state: str,
    trigger: str,
    actor: str,
    arbiter: str | None,
    round_no: int,
    deadlock_flagged: bool,
    resolution_decision_id: uuid.UUID | None,
    note: str | None,
) -> ConflictOut:
    """Move the conflict to ``to_state`` and append the audit row atomically. The
    current state lives on ``conflicts``; the append-only ``conflict_transitions``
    is the immutable history (TITAN-AGT-019)."""
    updated = await repo.update_conflict(
        conn,
        conflict.id,
        state=to_state,
        round_no=round_no,
        arbiter=arbiter,
        deadlock_flagged=deadlock_flagged,
        resolution_decision_id=resolution_decision_id,
    )
    await repo.insert_transition(
        conn,
        transition_id=uuid7(),
        conflict_row_id=conflict.id,
        from_state=conflict.state,
        to_state=to_state,
        trigger=trigger,
        actor=actor,
        round_no=round_no,
        note=note,
    )
    return ConflictOut.model_validate(updated)


# --- opening + advancing the §7 state machine ------------------------------


async def open_conflict(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    conflict_id: uuid.UUID,
    kind: DisputeKind,
    subject: str,
    parties: list[str],
    opened_by: str,
) -> ConflictOut:
    """Open a dispute in the mandatory ``direct`` round (§7.1). Idempotent on
    ``(project, conflict_id)`` — re-opening the same thread returns the live row so
    a replayed ConflictResolution message never forks the state."""
    if kind not in ("technical", "scope", "priority", "cross_domain"):
        raise _problem(422, "Unknown dispute kind", f"'{kind}' is not a valid dispute kind.")
    existing = await repo.get_conflict_by_cid(conn, project_id, conflict_id)
    if existing is not None:
        return ConflictOut.model_validate(existing)
    conflict_row_id = uuid7()
    row = await repo.insert_conflict(
        conn,
        conflict_row_id=conflict_row_id,
        project_id=project_id,
        conflict_id=conflict_id,
        kind=kind,
        subject=subject,
        parties=json.dumps(parties),
        state="direct",
        round_no=1,
        arbiter=None,
        opened_by=opened_by,
    )
    await repo.insert_transition(
        conn,
        transition_id=uuid7(),
        conflict_row_id=conflict_row_id,
        from_state=None,
        to_state="direct",
        trigger="opened",
        actor=opened_by,
        round_no=1,
        note=subject,
    )
    return ConflictOut.model_validate(row)


async def record_round(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    actor: str,
    note: str | None = None,
) -> ConflictOut:
    """Record another exchange at the current level without progress (§7.1 parties
    still disagree). Increments the round counter — the fuel of the rule-5 round
    deadlock — but does not change the level."""
    conflict = await _load(conn, project_id, conflict_id)
    if is_terminal(conflict.state):
        raise _problem(
            409, "Conflict already resolved", "Cannot add a round to a resolved conflict."
        )
    return await _persist_transition(
        conn,
        conflict,
        to_state=conflict.state,
        trigger="no_consensus",
        actor=actor,
        arbiter=conflict.arbiter,
        round_no=conflict.round + 1,
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=conflict.resolution_decision_id,
        note=note,
    )


async def resolve_by_consensus(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    decision: ResolutionDecision,
    actor: str,
) -> ConflictOut:
    """Rule 1: the parties reached consensus in the DIRECT round — the common case.
    Records the agreement as a DecisionLog (authority ``consensus``). Only valid from
    ``direct``: once a dispute has an arbiter or reached the CEO, it is resolved by the
    binding verdict / executive ruling, never re-closed as a mere consensus (a party
    could otherwise self-close its own arbitration, corrupting the authority stamp)."""
    conflict = await _load(conn, project_id, conflict_id)
    if conflict.state != "direct":
        raise _problem(
            409,
            "Consensus only in the direct round",
            f"A {conflict.state} conflict is resolved by its arbiter/CEO, not a consensus.",
        )
    _require_legal(conflict.state, "resolved")
    dec = await _record_decision(conn, project_id, decision, authority="consensus", actor=actor)
    return await _persist_transition(
        conn,
        conflict,
        to_state="resolved",
        trigger="consensus",
        actor=actor,
        arbiter=conflict.arbiter,
        round_no=conflict.round,
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=dec.id,
        note="direct-round consensus",
    )


async def escalate_to_arbitration(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    actor: str = "orchestrator",
) -> ConflictOut:
    """Rule 2: no consensus after the direct round → the Orchestrator appoints the
    arbiter by the dispute's kind (Architect/PM Product/PM Project). The appointment
    is mechanical — the disputing parties never choose their own judge (§7.2)."""
    conflict = await _load(conn, project_id, conflict_id)
    _require_legal(conflict.state, "arbitration")
    arbiter = arbiter_for(conflict.kind)
    if arbiter is None:
        raise _problem(
            409,
            "No arbiter for a cross-domain dispute",
            "Cross-domain disputes escalate to the CEO (executive), not arbitration.",
        )
    return await _persist_transition(
        conn,
        conflict,
        to_state="arbitration",
        trigger="no_consensus",
        actor="orchestrator",
        arbiter=arbiter,
        # A new level starts a fresh round budget (round counts exchanges AT the
        # current level). So escalating is progress — it never trips the rule-5
        # round deadlock, which only fires when ONE level is re-argued to exhaustion.
        round_no=1,
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=conflict.resolution_decision_id,
        note=f"orchestrator appointed {arbiter} (kind={conflict.kind})",
    )


async def record_arbitration_verdict(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    decision: ResolutionDecision,
    actor: str | None = None,
) -> ConflictOut:
    """Rule 2: the arbiter's verdict is binding — resolve with an ``arbitration``
    DecisionLog. Dispute the verdict via :func:`escalate_to_executive` instead."""
    conflict = await _load(conn, project_id, conflict_id)
    if conflict.state != "arbitration":
        raise _problem(409, "Not in arbitration", "Only an arbitrating conflict takes a verdict.")
    _require_legal("arbitration", "resolved")
    who = actor or conflict.arbiter or "orchestrator"
    dec = await _record_decision(conn, project_id, decision, authority="arbitration", actor=who)
    return await _persist_transition(
        conn,
        conflict,
        to_state="resolved",
        trigger="arbiter_verdict",
        actor=who,
        arbiter=conflict.arbiter,
        round_no=conflict.round,
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=dec.id,
        note="binding arbiter verdict",
    )


async def escalate_to_executive(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    actor: str = "orchestrator",
) -> ConflictOut:
    """Rule 3: escalate to the CEO. Only two §7.3 paths are valid, and the state
    machine — not the caller — enforces which one applies (TITAN-AGT-017):
    ``arbitration → executive`` is a disputed verdict; ``direct → executive`` is the
    cross-domain shortcut and is REFUSED for a dispute that has a valid arbiter (a
    technical/scope/priority dispute must be arbitrated first, §7.2). The trigger is
    derived from the state so the audit can never mislabel an arbitrable dispute as
    cross-domain."""
    conflict = await _load(conn, project_id, conflict_id)
    _require_legal(conflict.state, "executive")
    if conflict.state == "direct" and arbiter_for(conflict.kind) is not None:
        raise _problem(
            409,
            "Arbitration required before executive escalation",
            f"A {conflict.kind} dispute must be arbitrated (§7.2) before reaching the CEO.",
        )
    trigger = "cross_domain" if conflict.state == "direct" else "disputed"
    return await _persist_transition(
        conn,
        conflict,
        to_state="executive",
        trigger=trigger,
        actor=actor,
        arbiter=conflict.arbiter,
        round_no=1,  # a new level starts a fresh round budget (see escalate_to_arbitration)
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=conflict.resolution_decision_id,
        note=f"escalated to CEO ({trigger})",
    )


async def record_executive_decision(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    decision: ResolutionDecision,
    actor: str = "ceo",
) -> ConflictOut:
    """Rule 3: the CEO's decision is final inside the platform — resolve with an
    ``executive`` DecisionLog."""
    conflict = await _load(conn, project_id, conflict_id)
    if conflict.state != "executive":
        raise _problem(409, "Not escalated", "Only an executive-level conflict takes a CEO ruling.")
    _require_legal("executive", "resolved")
    dec = await _record_decision(conn, project_id, decision, authority="executive", actor=actor)
    return await _persist_transition(
        conn,
        conflict,
        to_state="resolved",
        trigger="executive_decision",
        actor=actor,
        arbiter=conflict.arbiter,
        round_no=conflict.round,
        deadlock_flagged=conflict.deadlock_flagged,
        resolution_decision_id=dec.id,
        note="final CEO decision",
    )


async def _record_decision(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    decision: ResolutionDecision,
    *,
    authority: str,
    actor: str,
) -> DecisionOut:
    return await create_decision(
        conn,
        project_id,
        DecisionCreate(
            title=decision.title,
            context=decision.context,
            decision=decision.decision,
            alternatives=decision.alternatives,
            consequences=decision.consequences,
            authority=authority,
            decided_by_agent_id=decision.decided_by_agent_id,
        ),
        actor=actor,
    )


async def get_conflict(
    conn: AsyncConnection, project_id: uuid.UUID, conflict_id: uuid.UUID
) -> ConflictOut | None:
    row = await repo.get_conflict_by_cid(conn, project_id, conflict_id)
    return ConflictOut.model_validate(row) if row is not None else None


async def list_transitions(
    conn: AsyncConnection, project_id: uuid.UUID, conflict_id: uuid.UUID
) -> list[ConflictTransitionOut]:
    """The append-only audit trail of one conflict, oldest first — the persisted
    proof the flow traversed direct → arbitration → executive (TITAN-AGT-017)."""
    conflict = await _load(conn, project_id, conflict_id)
    rows = await repo.list_transitions(conn, conflict.id)
    return [ConflictTransitionOut.model_validate(r) for r in rows]


# --- security veto (TITAN-AGT-018) -----------------------------------------


async def raise_veto(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    artifact_ref: str,
    raised_by: str,
    reason: str,
    task_id: uuid.UUID | None = None,
    veto_message_id: uuid.UUID | None = None,
) -> MergeVetoOut:
    """Record a Security veto against an artifact. Idempotent: a second veto on an
    already-vetoed artifact returns the live one (the partial-unique index is the
    guard, so a concurrent duplicate is caught and folded, not surfaced)."""
    existing = await repo.get_active_veto_by_ref(conn, project_id, artifact_ref)
    if existing is not None:
        return MergeVetoOut.model_validate(existing)
    try:
        # A SAVEPOINT isolates the unique-violation: without it, the failed INSERT
        # aborts the whole transaction and the fold-query below would itself raise
        # (InFailedSqlTransaction) — and, on the runtime emit path, roll back the
        # message we just sent. The nested block rolls back to the savepoint only,
        # leaving the outer transaction usable so the concurrent winner is returned.
        async with conn.begin_nested():
            row = await repo.insert_veto(
                conn,
                veto_id=uuid7(),
                project_id=project_id,
                artifact_ref=artifact_ref,
                task_id=task_id,
                raised_by=raised_by,
                veto_message_id=veto_message_id,
                reason=reason,
            )
    except IntegrityError:
        # Lost a race to another veto of the same artifact — fold onto the winner.
        current = await repo.get_active_veto_by_ref(conn, project_id, artifact_ref)
        if current is None:
            raise
        return MergeVetoOut.model_validate(current)
    return MergeVetoOut.model_validate(row)


async def is_merge_blocked(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    artifact_ref: str | None = None,
    task_id: uuid.UUID | None = None,
) -> bool:
    """Whether an active veto blocks merging this artifact/task. Checks ONLY the
    veto table — no approval, not even the CEO's, is consulted, so no approval path
    can bypass it (§7.4, TITAN-AGT-018)."""
    if artifact_ref is not None and (
        await repo.get_active_veto_by_ref(conn, project_id, artifact_ref) is not None
    ):
        return True
    return task_id is not None and bool(await repo.active_vetoes_for_task(conn, task_id))


async def assert_merge_allowed(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    artifact_ref: str | None = None,
    task_id: uuid.UUID | None = None,
) -> None:
    """Raise a 409 if a Security veto blocks the merge/release. The mandatory gate
    on every merge path — including a CEO-approved release job (TITAN-AGT-018)."""
    if await is_merge_blocked(conn, project_id, artifact_ref=artifact_ref, task_id=task_id):
        raise _problem(
            409,
            "Blocked by security veto",
            "A Security veto blocks this merge/release; only a human override lifts it.",
        )


async def override_veto(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    artifact_ref: str,
    risk_memo: str,
    decided_by_agent_id: uuid.UUID | None = None,
) -> tuple[MergeVetoOut, DecisionOut]:
    """The ONLY path that lifts a veto: the human client's override, relayed by the
    CEO as a structured risk memo (§7.4). Records a permanent ``human_override``
    DecisionLog (append-only, TITAN-AGT-019) and flips the veto to ``overridden``."""
    veto = await repo.get_active_veto_by_ref(conn, project_id, artifact_ref)
    if veto is None:
        raise _problem(404, "No active veto", f"No active veto on {artifact_ref} to override.")
    decision = await create_decision(
        conn,
        project_id,
        DecisionCreate(
            title=f"Human override of security veto on {artifact_ref}",
            context=risk_memo,
            decision="Override the Security veto and permit merge/release of the artifact.",
            alternatives="Uphold the veto and keep the artifact blocked.",
            consequences="Accepted risk recorded permanently; the pipeline may proceed.",
            authority="human_override",
            decided_by_agent_id=decided_by_agent_id,
        ),
        actor="ceo",
    )
    updated = await repo.mark_veto_overridden(conn, veto.id, override_decision_id=decision.id)
    if updated is None:  # concurrent override — the other winner already lifted it
        raise _problem(409, "Veto already overridden", "This veto was just overridden.")
    return MergeVetoOut.model_validate(updated), decision


async def capture_veto_verdict(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    message: AgentMessage,
    *,
    vetoing_roles: frozenset[str],
    trigger: AgentMessage | None = None,
) -> MergeVetoOut | None:
    """The runtime hook (Phase 29 emit path): when a veto-holding agent emits a
    ReviewRequest reply carrying a ``veto`` verdict, record the pipeline veto. Any
    other message is a no-op. Keyed on the artifact_ref so it is idempotent with
    :func:`raise_veto`. Returns the veto, or None if the message is not a veto.

    The veto is linked to the reviewed TASK from the ``trigger`` (the ReviewRequest the
    agent is answering) — that task_id is the authoritative subject the merge gate
    (approve → done) keys on, and it comes from the trusted inbound request, NOT from
    the model's reply (which could echo a wrong or non-existent context_task_id). So a
    Security veto on a task-scoped review ALWAYS blocks that task's merge
    (TITAN-AGT-018), and a veto invisible to its merge path cannot arise. A veto with
    no reviewed task (e.g. a release) is artifact-only and enforced by artifact_ref."""
    if message.type != "ReviewRequest" or message.from_agent not in vetoing_roles:
        return None
    verdict = message.payload.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("decision") != "veto":
        return None
    artifact_ref = message.payload.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref:
        return None
    task_id = _reviewed_task_id(trigger)
    # A ReviewRequest's context_task_id is a body field validated only as a UUID
    # (§6.1), never against the tasks table — so a dangling/hallucinated ref would
    # FK-violate merge_vetoes.task_id, roll back the whole turn emission, and
    # poison-loop. Degrade a non-existent reference to an artifact-only veto (still
    # enforced by artifact_ref) so the veto is ALWAYS recorded, never lost.
    if task_id is not None and not await repo.task_exists(conn, task_id):
        task_id = None
    return await raise_veto(
        conn,
        project_id,
        artifact_ref=artifact_ref,
        raised_by=message.from_agent,
        reason=f"Security veto by {message.from_agent} on {artifact_ref}",
        task_id=task_id,
        veto_message_id=message.message_id,
    )


def _reviewed_task_id(msg: AgentMessage | None) -> uuid.UUID | None:
    """The task a ReviewRequest is about: its envelope task_id, else the
    ``context_task_id`` in its payload (§6.2)."""
    if msg is None:
        return None
    if msg.task_id is not None:
        return msg.task_id
    return _as_uuid(msg.payload.get("context_task_id"))


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


# --- deadlock (rule 5): detection hook + live thresholds (TITAN-AGT-020) ----


async def get_thresholds(conn: AsyncConnection, project_id: uuid.UUID) -> DeadlockThresholds:
    """The effective deadlock thresholds: the project row if set, else the org
    default row, else the code defaults. Read fresh every call (never cached), so a
    change takes effect on the next detection — no redeploy (TITAN-AGT-020)."""
    row = await repo.get_thresholds_for_project(conn, project_id)
    if row is None:
        row = await repo.get_global_thresholds(conn)
    if row is None:
        return DeadlockThresholds()
    return DeadlockThresholds(
        max_conflict_rounds=row.max_conflict_rounds, max_task_reopens=row.max_task_reopens
    )


async def set_thresholds(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID | None,
    max_conflict_rounds: int,
    max_task_reopens: int,
) -> ThresholdsOut:
    """Set the deadlock thresholds for a project (or the org default when
    ``project_id`` is None). Takes effect immediately (TITAN-AGT-020)."""
    if max_conflict_rounds < 1 or max_task_reopens < 1:
        raise _problem(422, "Invalid threshold", "Thresholds must be >= 1.")
    row = await repo.upsert_thresholds(
        conn,
        threshold_id=uuid7(),
        project_id=project_id,
        max_conflict_rounds=max_conflict_rounds,
        max_task_reopens=max_task_reopens,
    )
    return ThresholdsOut.model_validate(row)


async def detect_conflict_deadlock(
    conn: AsyncConnection, project_id: uuid.UUID, conflict_id: uuid.UUID
) -> DeadlockSignal | None:
    """Rule 5 detection hook (the Phase-32 watchdog calls this): a live conflict
    that has reached ``max_conflict_rounds`` without resolving. Pure detection — the
    caller applies the escalation via :func:`apply_deadlock_escalation`."""
    conflict = await repo.get_conflict_by_cid(conn, project_id, conflict_id)
    if conflict is None or is_terminal(conflict.state):
        return None
    thresholds = await get_thresholds(conn, project_id)
    if not round_deadlocked(conflict.round, thresholds):
        return None
    return DeadlockSignal(
        scope="conflict_rounds",
        subject_id=conflict.id,
        current=conflict.round,
        threshold=thresholds.max_conflict_rounds,
        escalate_to=_deadlock_target(conflict.state, conflict.kind),
        reason=f"conflict reached round {conflict.round} without resolution",
    )


async def detect_reopen_deadlock(
    conn: AsyncConnection, project_id: uuid.UUID, task_id: uuid.UUID
) -> DeadlockSignal | None:
    """Rule 5 detection hook: a task reopened ``max_task_reopens`` times."""
    count = await repo.count_task_reopens(conn, task_id)
    thresholds = await get_thresholds(conn, project_id)
    if not reopens_deadlocked(count, thresholds):
        return None
    return DeadlockSignal(
        scope="task_reopens",
        subject_id=task_id,
        current=count,
        threshold=thresholds.max_task_reopens,
        escalate_to=None,
        reason=f"task reopened {count} times",
    )


def _deadlock_target(state: str, kind: str) -> str:
    """The state a deadlock escalates to, honoring the cross-domain shortcut: a
    cross-domain dispute stuck in the direct round jumps straight to the CEO
    (executive) rather than an arbitration with no valid arbiter."""
    nxt = escalate_one_level(state)
    if nxt == "arbitration" and arbiter_for(kind) is None:
        return "executive"
    return nxt


async def apply_deadlock_escalation(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    actor: str = "orchestrator",
) -> ConflictOut:
    """Apply rule 5: escalate the conflict one level and flag it for the dashboard.
    This is a safety escalation OUTSIDE the normal §7 ladder (it may skip a level for
    a cross-domain dispute), so it bypasses the ``ALLOWED_TRANSITIONS`` guard by
    design; the transition is still recorded in the append-only audit."""
    conflict = await _load(conn, project_id, conflict_id)
    if is_terminal(conflict.state):
        raise _problem(409, "Conflict already resolved", "A resolved conflict cannot deadlock.")
    target = _deadlock_target(conflict.state, conflict.kind)
    arbiter = conflict.arbiter
    if target == "arbitration" and arbiter is None:
        arbiter = arbiter_for(conflict.kind)
    return await _persist_transition(
        conn,
        conflict,
        to_state=target,
        trigger="deadlock_escalation",
        actor=actor,
        arbiter=arbiter,
        # Reset the round budget for the escalated level, so the newly-appointed
        # arbiter (or the CEO) gets a turn before detection can fire again — the
        # escalation moves exactly ONE level, never cascading past the arbiter (§7.5).
        round_no=1,
        deadlock_flagged=True,
        resolution_decision_id=conflict.resolution_decision_id,
        note=f"deadlock at round {conflict.round}: escalated {conflict.state} -> {target}",
    )


# --- authority contract (TITAN-AGT-011) ------------------------------------


def authority_contract(definition: AgentDefinition) -> AuthorityContract:
    """One agent's mechanically-checkable authority facts (veto/co-sign/fail-mode)."""
    return contract_for(definition)


def all_authority_contracts(registry: AgentRegistry) -> list[AuthorityContract]:
    """The authority contract for every agent in the roster — the §4 contract-test
    surface (TITAN-AGT-011)."""
    return [contract_for(d) for d in registry.all()]


def vetoing_roles(registry: AgentRegistry) -> frozenset[str]:
    """The roles that hold a blocking veto (exactly ``{"security"}`` by default) —
    the set the runtime veto-capture hook consults, data-driven from the roster."""
    return vetoing_agents(registry.all())


__all__ = [
    "AuthorityContract",
    "CoSignRequirement",
    "ConflictOut",
    "ConflictTransitionOut",
    "DeadlockSignal",
    "DeadlockThresholds",
    "DisputeKind",
    "MergeVetoOut",
    "ResolutionDecision",
    "ThresholdsOut",
    "all_authority_contracts",
    "apply_deadlock_escalation",
    "assert_merge_allowed",
    "authority_contract",
    "capture_veto_verdict",
    "detect_conflict_deadlock",
    "detect_reopen_deadlock",
    "escalate_to_arbitration",
    "escalate_to_executive",
    "get_conflict",
    "get_thresholds",
    "is_merge_blocked",
    "list_transitions",
    "open_conflict",
    "override_veto",
    "raise_veto",
    "record_arbitration_verdict",
    "record_executive_decision",
    "record_round",
    "resolve_by_consensus",
    "set_thresholds",
    "vetoing_roles",
]
