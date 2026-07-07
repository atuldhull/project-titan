"""The persisted §7 conflict-resolution state machine + Security veto gate — needs
the disposable test DB (Vol 2 §7, TITAN-AGT-017/018/019).

Covers the DoD: a simulated dispute traverses direct → arbitration → executive with
persisted state transitions; a veto blocks merge and only a human override lifts it;
the conflict audit trail is append-only; the runtime veto-capture hook records a
Security veto emitted through the normal outbox."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import run_async
from titan.conflict import service as conflict
from titan.conflict.service import ResolutionDecision
from titan.gateway.problems import ProblemException
from titan.messaging import service as messaging
from titan.messaging.protocol import AgentMessage

pytestmark = pytest.mark.schema


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, :d, 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "d": "P", "u": uid},
        )
    return pid


def _resolution(summary: str) -> ResolutionDecision:
    return ResolutionDecision(title=f"Resolution: {summary}", context="dispute", decision=summary)


# --- the direct → arbitration → executive traversal (TITAN-AGT-017) --------


def test_dispute_traverses_direct_arbitration_executive(migrated_engine: Any) -> None:
    """The simulated-dispute DoD: every level is persisted, the arbiter is appointed
    mechanically, and the final CEO ruling is an executive DecisionLog."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()

        async with migrated_engine.begin() as conn:
            opened = await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="sync vs async payment integration",
                parties=["eng_backend", "eng_frontend"],
                opened_by="eng_backend",
            )
        assert opened.state == "direct" and opened.round == 1 and opened.arbiter is None

        # Rule 2: no consensus → the Orchestrator appoints the arbiter by kind.
        # Escalating is progress, so the round budget resets for the new level.
        async with migrated_engine.begin() as conn:
            arb = await conflict.escalate_to_arbitration(conn, pid, cid)
        assert arb.state == "arbitration" and arb.arbiter == "architect" and arb.round == 1

        # Rule 3: the disputed arbitration escalates to the CEO (trigger derived from
        # the state — never mislabeled).
        async with migrated_engine.begin() as conn:
            exe = await conflict.escalate_to_executive(conn, pid, cid)
        assert exe.state == "executive" and exe.round == 1

        # Rule 3: the CEO's decision is final and binds as a DecisionLog.
        async with migrated_engine.begin() as conn:
            done = await conflict.record_executive_decision(
                conn, pid, cid, decision=_resolution("go async")
            )
        assert done.state == "resolved" and done.resolution_decision_id is not None

        # The persisted transitions ARE the audit — the flow, not a prompt, drove it.
        async with migrated_engine.connect() as conn:
            trail = await conflict.list_transitions(conn, pid, cid)
        assert [(t.from_state, t.to_state, t.trigger) for t in trail] == [
            (None, "direct", "opened"),
            ("direct", "arbitration", "no_consensus"),
            ("arbitration", "executive", "disputed"),
            ("executive", "resolved", "executive_decision"),
        ]

        # The binding resolution is recorded with authority=executive.
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT authority FROM decisions WHERE id = :id"),
                {"id": done.resolution_decision_id},
            )
        assert row.scalar_one() == "executive"

    run_async(scenario)


def test_binding_arbiter_verdict_resolves_without_the_ceo(migrated_engine: Any) -> None:
    """Rule 2: most disputes end at arbitration — the arbiter's verdict binds and is
    an ``arbitration`` DecisionLog, never touching the executive level."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="scope",
                subject="cut story S-9?",
                parties=["pm_product", "eng_backend"],
                opened_by="pm_product",
            )
        async with migrated_engine.begin() as conn:
            arb = await conflict.escalate_to_arbitration(conn, pid, cid)
        assert arb.arbiter == "pm_product"  # scope dispute → PM Product
        async with migrated_engine.begin() as conn:
            resolved = await conflict.record_arbitration_verdict(
                conn, pid, cid, decision=_resolution("keep S-9")
            )
        assert resolved.state == "resolved"
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT authority FROM decisions WHERE id = :id"),
                {"id": resolved.resolution_decision_id},
            )
        assert row.scalar_one() == "arbitration"

    run_async(scenario)


def test_cross_domain_dispute_cannot_be_arbitrated(migrated_engine: Any) -> None:
    """A cross-domain dispute has no arbiter — arbitration is refused; it must go
    straight to the CEO (§7.3)."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="cross_domain",
                subject="platform-wide",
                parties=["security", "eng_devops"],
                opened_by="orchestrator",
            )
        with pytest.raises(ProblemException) as ei:
            async with migrated_engine.begin() as conn:
                await conflict.escalate_to_arbitration(conn, pid, cid)
        assert ei.value.status == 409
        # But it CAN escalate directly to executive (the cross-domain shortcut §7.3).
        async with migrated_engine.begin() as conn:
            exe = await conflict.escalate_to_executive(conn, pid, cid)
        assert exe.state == "executive"

    run_async(scenario)


def test_arbitrable_dispute_cannot_skip_arbitration_to_executive(migrated_engine: Any) -> None:
    """The state machine refuses the direct→executive shortcut for a dispute that has
    a valid arbiter (§7.2 arbitration is mandatory first) — TITAN-AGT-017 enforcement,
    not caller compliance. Only cross-domain disputes may skip it."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        for kind in ("technical", "scope", "priority"):
            cid = uuid.uuid4()
            async with migrated_engine.begin() as conn:
                await conflict.open_conflict(
                    conn,
                    pid,
                    conflict_id=cid,
                    kind=kind,
                    subject="x",
                    parties=["a", "b"],
                    opened_by="a",
                )
            with pytest.raises(ProblemException) as ei:
                async with migrated_engine.begin() as conn:
                    await conflict.escalate_to_executive(conn, pid, cid)
            assert ei.value.status == 409, f"{kind} should require arbitration first"

    run_async(scenario)


def test_consensus_only_from_direct_round(migrated_engine: Any) -> None:
    """resolve_by_consensus is refused once a dispute has an arbiter or reached the
    CEO — a party cannot self-close its own arbitration as a mere consensus."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="x",
                parties=["a", "b"],
                opened_by="a",
            )
            await conflict.escalate_to_arbitration(conn, pid, cid)
        with pytest.raises(ProblemException) as ei:
            async with migrated_engine.begin() as conn:
                await conflict.resolve_by_consensus(
                    conn, pid, cid, decision=_resolution("sneak past arbiter"), actor="a"
                )
        assert ei.value.status == 409

    run_async(scenario)


def test_open_conflict_is_idempotent(migrated_engine: Any) -> None:
    """A replayed ConflictResolution never forks the state — re-opening the same
    (project, conflict_id) returns the live row."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            first = await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="priority",
                subject="which sprint?",
                parties=["pm_project", "eng_backend"],
                opened_by="pm_project",
            )
        async with migrated_engine.begin() as conn:
            await conflict.escalate_to_arbitration(conn, pid, cid)
        async with migrated_engine.begin() as conn:
            again = await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="priority",
                subject="which sprint?",
                parties=["pm_project", "eng_backend"],
                opened_by="pm_project",
            )
        # Same row, and the arbitration progress was NOT reset by re-opening.
        assert again.id == first.id and again.state == "arbitration"

    run_async(scenario)


def test_illegal_transition_is_refused(migrated_engine: Any) -> None:
    """The state machine — not prompt compliance — decides what is reachable: a
    resolved conflict cannot take a CEO ruling, and executive is unreachable twice."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="x",
                parties=["a", "b"],
                opened_by="a",
            )
            await conflict.resolve_by_consensus(
                conn, pid, cid, decision=_resolution("agreed"), actor="a"
            )
        # Now resolved — a further ruling is a 409, not a silent no-op.
        with pytest.raises(ProblemException) as ei:
            async with migrated_engine.begin() as conn:
                await conflict.record_executive_decision(
                    conn, pid, cid, decision=_resolution("late")
                )
        assert ei.value.status == 409

    run_async(scenario)


# --- security veto gate (TITAN-AGT-018) ------------------------------------


def test_veto_blocks_merge_and_only_human_override_lifts_it(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        async with migrated_engine.begin() as conn:
            veto = await conflict.raise_veto(
                conn, pid, artifact_ref="pr:42", raised_by="security", reason="SQL injection"
            )
        assert veto.status == "active"

        async with migrated_engine.connect() as conn:
            assert await conflict.is_merge_blocked(conn, pid, artifact_ref="pr:42") is True
        with pytest.raises(ProblemException) as ei:
            async with migrated_engine.connect() as conn:
                await conflict.assert_merge_allowed(conn, pid, artifact_ref="pr:42")
        assert ei.value.status == 409

        # Re-vetoing the same artifact is idempotent (one active veto).
        async with migrated_engine.begin() as conn:
            again = await conflict.raise_veto(
                conn, pid, artifact_ref="pr:42", raised_by="security", reason="still bad"
            )
        assert again.id == veto.id

        # Only the human override (relayed by the CEO) lifts it, as a permanent
        # human_override DecisionLog (append-only).
        async with migrated_engine.begin() as conn:
            lifted, decision = await conflict.override_veto(
                conn, pid, artifact_ref="pr:42", risk_memo="client accepts the risk"
            )
        assert lifted.status == "overridden"
        assert decision.authority == "human_override"
        assert lifted.override_decision_id == decision.id

        async with migrated_engine.connect() as conn:
            assert await conflict.is_merge_blocked(conn, pid, artifact_ref="pr:42") is False

    run_async(scenario)


def test_override_without_active_veto_is_404(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        with pytest.raises(ProblemException) as ei:
            async with migrated_engine.begin() as conn:
                await conflict.override_veto(conn, pid, artifact_ref="pr:none", risk_memo="x")
        assert ei.value.status == 404

    run_async(scenario)


def test_runtime_captures_security_veto_verdict(migrated_engine: Any) -> None:
    """The Phase-29 emit hook: a Security ReviewRequest reply carrying a veto verdict
    becomes a pipeline veto; a non-veto verdict or a non-veto-holder is a no-op."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        veto_roles = frozenset({"security"})

        veto_msg = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="security",
            to_agent="eng_devops",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:77",
                "verdict": {"decision": "veto", "comments": []},
            },
        )
        # The ReviewRequest the security agent is answering names the reviewed task
        # (context_task_id) — the veto MUST link to it so the task's merge is blocked,
        # even though the security reply itself carries no task_id (TITAN-AGT-018).
        reviewed_task = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :p, 'reviewed')"),
                {"id": reviewed_task, "p": pid},
            )
        trigger = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="eng_devops",
            to_agent="security",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:77",
                "context_task_id": str(reviewed_task),
                "verdict": None,
            },
        )
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, veto_msg, actor="security")
            captured = await conflict.capture_veto_verdict(
                conn, pid, veto_msg, vetoing_roles=veto_roles, trigger=trigger
            )
        assert captured is not None and captured.artifact_ref == "pr:77"
        assert captured.raised_by == "security"
        assert captured.veto_message_id == veto_msg.message_id
        # Linked to the reviewed task, so the task-keyed merge gate will see it.
        assert captured.task_id == reviewed_task
        async with migrated_engine.connect() as conn:
            assert await conflict.is_merge_blocked(conn, pid, task_id=reviewed_task) is True

        # An approve verdict from Security is NOT a veto.
        approve_msg = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="security",
            to_agent="eng_devops",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:88",
                "verdict": {"decision": "approve", "comments": []},
            },
        )
        # A veto from a non-veto-holder (reviewer) is ignored by the gate.
        impostor_msg = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="reviewer",
            to_agent="eng_devops",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:99",
                "verdict": {"decision": "veto", "comments": []},
            },
        )
        async with migrated_engine.begin() as conn:
            assert (
                await conflict.capture_veto_verdict(
                    conn, pid, approve_msg, vetoing_roles=veto_roles
                )
                is None
            )
            assert (
                await conflict.capture_veto_verdict(
                    conn, pid, impostor_msg, vetoing_roles=veto_roles
                )
                is None
            )

    run_async(scenario)


def test_veto_capture_with_dangling_task_ref_degrades_not_crashes(migrated_engine: Any) -> None:
    """A trigger whose context_task_id names a NON-EXISTENT task must not FK-violate
    (which would roll back the whole turn emission and poison-loop). The veto is
    recorded artifact-only (task_id NULL) and still enforced by artifact_ref."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        veto_msg = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="security",
            to_agent="eng_devops",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:dangling",
                "verdict": {"decision": "veto", "comments": []},
            },
        )
        # A well-formed UUID that matches no task row (hallucinated / stale reference).
        trigger = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="eng_devops",
            to_agent="security",
            payload={
                "artifact_type": "pull_request",
                "artifact_ref": "pr:dangling",
                "context_task_id": str(uuid.uuid4()),
                "verdict": None,
            },
        )
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, veto_msg, actor="security")
            captured = await conflict.capture_veto_verdict(
                conn, pid, veto_msg, vetoing_roles=frozenset({"security"}), trigger=trigger
            )
        assert captured is not None and captured.task_id is None  # degraded, not crashed
        async with migrated_engine.connect() as conn:
            assert await conflict.is_merge_blocked(conn, pid, artifact_ref="pr:dangling") is True

    run_async(scenario)


# --- the append-only audit (TITAN-AGT-019) ---------------------------------


def test_conflict_transitions_are_db_append_only(migrated_engine: Any) -> None:
    """UPDATE/DELETE on a conflict transition row is rejected at the DB layer — the
    state-machine history cannot be rewritten (TITAN-AGT-019)."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="x",
                parties=["a", "b"],
                opened_by="a",
            )
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT ct.id FROM conflict_transitions ct JOIN conflicts c "
                    "ON ct.conflict_row_id = c.id WHERE c.conflict_id = :cid"
                ),
                {"cid": cid},
            )
            tid = row.scalar_one()

        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE conflict_transitions SET to_state = 'resolved' WHERE id = :id"),
                    {"id": tid},
                )
        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM conflict_transitions WHERE id = :id"), {"id": tid}
                )

    run_async(scenario)
