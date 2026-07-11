"""Phase 41 — the Vol 6 §2.5 delivery task states mapped onto the ONE task
machine (TITAN-WFL-009).

Covers: the new §2.5 edges (block/unblock, enter_loop → merge → complete,
fail → escalate, in_loop → escalate, requeue) are legal; the illegal
`BACKLOG → MERGED` is rejected and logged; the `merged → done` `complete`
transition enforces the acceptance gate (a schema-valid reflection); a Kanban
event is emitted on every state change; and budget hold pauses assignment and
resumes on a budget raise.
"""

import json
import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.orchestrator import service as orch
from titan.orchestrator import transitions

pytestmark = pytest.mark.schema

_MB = {"provider": "ollama", "model": "q"}
_REFLECTION_KEYS = (
    "goal",
    "what_worked",
    "what_surprised",
    "do_differently",
    "org_should_remember",
)


# --- pure transition-table unit tests --------------------------------------


def test_new_delivery_edges_resolve() -> None:
    legal = [
        ("ready", "block", "blocked"),
        ("blocked", "unblock", "ready"),
        ("in_progress", "enter_loop", "in_loop"),
        ("in_loop", "merge", "merged"),
        ("merged", "complete", "done"),
        ("in_progress", "fail", "failed"),
        ("failed", "escalate", "escalated"),
        ("in_loop", "escalate", "escalated"),
        ("reopened", "requeue", "ready"),
    ]
    for frm, action, to in legal:
        t = transitions.resolve(frm, action)
        assert t is not None and t.to_status == to, (frm, action)


def test_illegal_backlog_to_merged_is_unresolvable() -> None:
    # No single action moves backlog → merged; the DoD's canonical illegal move.
    assert transitions.resolve("backlog", "merge") is None
    assert transitions.resolve("backlog", "complete") is None


def test_wfl_lane_projection() -> None:
    assert transitions.wfl_lane("backlog") == "BACKLOG"
    assert transitions.wfl_lane("assigned") == "IN_PROGRESS"
    assert transitions.wfl_lane("in_review") == "IN_LOOP"
    assert transitions.wfl_lane("in_loop") == "IN_LOOP"
    assert transitions.wfl_lane("merged") == "MERGED"
    assert transitions.wfl_lane("escalated") == "ESCALATED"
    # Budget-hold overlay wins, but never mutates the underlying status.
    assert transitions.wfl_lane("in_progress", budget_hold=True) == "BUDGET_HOLD"


# --- integration helpers ----------------------------------------------------


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _task(api: dict[str, Any], pid: str) -> str:
    r = api["client"].post(
        f"/api/v1/projects/{pid}/tasks", json={"title": "work"}, headers=api["token_for"]("owner")
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _agent(api: dict[str, Any], role: str = "backend") -> str:
    r = api["client"].post(
        "/api/v1/agents",
        json={"role": role, "name": f"{role}-bot", "model_binding": _MB},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _transition(api: dict[str, Any], tid: str, action: str, *, actor: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if actor is not None:
        body["actor_agent_id"] = actor
    return api["client"].post(
        f"/api/v1/tasks/{tid}:transition", json=body, headers=api["token_for"]("owner")
    )


def _seed_reflection(api: dict[str, Any], pid: str, tid: str) -> None:
    """Insert a schema-valid reflection so the §2.5 acceptance gate passes."""
    metadata = {"record_subtype": "reflection", **{k: "x" for k in _REFLECTION_KEYS}}

    async def _do() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_records "
                    "(id, kind, scope, project_id, source_agent, source_task_id, title, body_md, "
                    "metadata, importance) VALUES (:id, 'episodic', :scope, :pid, 'a', :tid, 't', "
                    "'b', CAST(:md AS jsonb), 0.5)"
                ),
                {
                    "id": uuid.uuid4(),
                    "scope": f"project:{pid}",
                    "pid": pid,
                    "tid": tid,
                    "md": json.dumps(metadata),
                },
            )

    run_async(_do)


# --- §2.5 edges over the real endpoint -------------------------------------


def test_illegal_backlog_to_merged_returns_409(api: dict[str, Any]) -> None:
    tid = _task(api, _project(api))  # backlog
    r = _transition(api, tid, "merge")
    assert r.status_code == 409
    assert r.json()["type"] == "https://titan.dev/errors/invalid-transition"


def test_block_and_unblock(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assert _transition(api, tid, "ready").json()["status"] == "ready"
    assert _transition(api, tid, "block").json()["status"] == "blocked"
    unblocked = _transition(api, tid, "unblock")
    assert unblocked.json()["status"] == "ready"
    assert unblocked.json()["wfl_lane"] == "READY"


def test_loop_to_merged_then_complete_needs_reflection(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=assignee).status_code == 200
    assert _transition(api, tid, "start").json()["status"] == "in_progress"
    assert _transition(api, tid, "enter_loop").json()["status"] == "in_loop"
    assert _transition(api, tid, "merge").json()["status"] == "merged"
    # Acceptance gate: `complete` without a reflection is a 409.
    blocked = _transition(api, tid, "complete")
    assert blocked.status_code == 409 and "acceptance" in blocked.json()["detail"].lower()
    # With a schema-valid reflection, `complete` reaches done.
    _seed_reflection(api, pid, tid)
    done = _transition(api, tid, "complete")
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert done.json()["wfl_lane"] == "DONE"
    assert done.json()["completed_at"] is not None


def test_fail_then_escalate(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=assignee).status_code == 200
    assert _transition(api, tid, "start").status_code == 200
    assert _transition(api, tid, "fail").json()["status"] == "failed"
    escalated = _transition(api, tid, "escalate")
    assert escalated.json()["status"] == "escalated"
    assert escalated.json()["wfl_lane"] == "ESCALATED"


def test_in_loop_can_escalate(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=assignee).status_code == 200
    assert _transition(api, tid, "start").status_code == 200
    assert _transition(api, tid, "enter_loop").status_code == 200
    assert _transition(api, tid, "escalate").json()["status"] == "escalated"


def test_reopened_can_requeue_to_ready(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=assignee).status_code == 200
    assert _transition(api, tid, "start").status_code == 200
    assert _transition(api, tid, "submit_review").status_code == 200
    assert _transition(api, tid, "approve", actor=reviewer).json()["status"] == "done"
    assert _transition(api, tid, "reopen").json()["status"] == "reopened"
    requeued = _transition(api, tid, "requeue")
    assert requeued.json()["status"] == "ready"


def test_task_out_exposes_wfl_lane(api: dict[str, Any]) -> None:
    tid = _task(api, _project(api))
    r = api["client"].get(f"/api/v1/tasks/{tid}", headers=api["token_for"]("owner"))
    assert r.status_code == 200 and r.json()["wfl_lane"] == "BACKLOG"


def test_kanban_event_emitted_on_every_transition(api: dict[str, Any]) -> None:
    """TITAN-WFL-009: a state change emits a Kanban event (into the outbox, whence
    the Phase-24 fan-out republishes it to the socket)."""
    pid = _project(api)
    tid = _task(api, pid)
    assert _transition(api, tid, "ready").status_code == 200

    async def _kanban_rows() -> list[dict[str, Any]]:
        async with api["engine"].connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT payload FROM outbox WHERE topic = :t ORDER BY created_at"),
                    {"t": "stream:events.domain"},
                )
            ).all()
        out = []
        for r in rows:
            wrapper = json.loads(r.payload) if isinstance(r.payload, str) else r.payload
            data = wrapper.get("data", {})
            if data.get("type") == "task.kanban":
                out.append(data["kanban"])
        return out

    cards = run_async(_kanban_rows)
    assert any(c["task_id"] == tid and c["lane"] == "READY" for c in cards)


# --- budget hold on task assignment (scheduler) ----------------------------


def test_budget_hold_pauses_assignment_and_resumes(api: dict[str, Any]) -> None:
    """A held task is not assigned (no LLM spend); raising the budget lets the
    scheduler assign it, resuming exactly where it paused. All engine work runs in
    ONE event loop (the fakeredis lock + the two claims must share one loop)."""
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    assert _transition(api, tid, "ready").status_code == 200
    engine = api["engine"]

    async def scenario() -> tuple[Any, Any]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
                    "VALUES (:id, 'task', :tid, 'task budget reached', 100)"
                ),
                {"id": uuid.uuid4(), "tid": uuid.UUID(tid)},
            )
        paused = await orch.claim_and_assign(engine, redis, uuid.UUID(tid), uuid.UUID(assignee))
        # Raise the budget (clear the hold) — exactly what resume_task_budget does.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM budget_holds WHERE scope = 'task' AND scope_id = :t"),
                {"t": uuid.UUID(tid)},
            )
        resumed = await orch.claim_and_assign(engine, redis, uuid.UUID(tid), uuid.UUID(assignee))
        await redis.aclose()
        return paused, resumed

    paused, resumed = run_async(scenario)
    assert paused is None  # held → not assigned
    assert resumed is not None and resumed.status == "assigned"  # resumed where it paused


def test_resume_task_budget_endpoint(api: dict[str, Any]) -> None:
    """The task budget-raise endpoint is authorized and returns the task."""
    tid = _task(api, _project(api))
    r = api["client"].post(f"/api/v1/tasks/{tid}:resume-budget", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == tid


def test_raise_budget_resets_the_spend_counter(api: dict[str, Any]) -> None:
    """Regression: raising the budget must clear the DB hold AND reset the Redis
    rolling-spend counter — else reserve() re-breaches the stale counter and
    immediately re-holds, so the resume would not stick for LLM work."""
    from titan.llm import budget
    from titan.llm import service as llm

    tid = _task(api, _project(api))

    async def scenario() -> tuple[bool, str | None]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        key = budget.spend_key("task", uuid.UUID(tid))
        await redis.set(key, "999999999")  # counter pinned at/over cap
        await redis.set(f"{key}:notified80", "1")
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
                    "VALUES (:id, 'task', :t, 'reached', 100)"
                ),
                {"id": uuid.uuid4(), "t": uuid.UUID(tid)},
            )
            await llm.raise_budget(conn, scope="task", scope_id=uuid.UUID(tid), redis=redis)
            hold = await llm.active_budget_hold(conn, project_id=None, task_id=uuid.UUID(tid))
        counter_after = await redis.get(key)
        await redis.aclose()
        return counter_after is None, hold

    counter_cleared, hold = run_async(scenario)
    assert counter_cleared  # spend counter reset so reserve() won't re-breach
    assert hold is None  # DB hold cleared too
