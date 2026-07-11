"""Phase 13 integration tests: the task orchestration engine — :transition
endpoint, scheduler dependent-promotion, race-safe assignment, review/reopen.

Covers the DoD: illegal (state, action) pairs -> 409; a 100-iteration race with
two concurrent claimers yields exactly one assigned winner; completing the last
blocker readies the dependent in the same transaction and enqueues it on
stream:tasks.ready; assignee == reviewer cannot reach done."""

import asyncio
import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.service import STREAM_TASKS_READY

pytestmark = pytest.mark.schema

_MB = {"provider": "ollama", "model": "q"}


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _task(api: dict[str, Any], project_id: str) -> str:
    r = api["client"].post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": "work"},
        headers=api["token_for"]("owner"),
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


def _transition(
    api: dict[str, Any],
    task_id: str,
    action: str,
    *,
    actor: str | None = None,
    comment: str | None = None,
    token: str = "owner",
) -> Any:
    body: dict[str, Any] = {"action": action}
    if actor is not None:
        body["actor_agent_id"] = actor
    if comment is not None:
        body["comment"] = comment
    return api["client"].post(
        f"/api/v1/tasks/{task_id}:transition", json=body, headers=api["token_for"](token)
    )


def _drive_to_in_review(api: dict[str, Any], task_id: str, assignee: str) -> None:
    assert _transition(api, task_id, "ready").status_code == 200
    assert _transition(api, task_id, "assign", actor=assignee).status_code == 200
    assert _transition(api, task_id, "start").status_code == 200
    assert _transition(api, task_id, "submit_review").status_code == 200


# --- happy path + guards ---------------------------------------------------


def test_full_lifecycle_backlog_to_done(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")

    ready = _transition(api, tid, "ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assigned = _transition(api, tid, "assign", actor=assignee)
    assert assigned.json()["status"] == "assigned"
    assert assigned.json()["assignee_agent_id"] == assignee
    assert _transition(api, tid, "start").json()["status"] == "in_progress"
    assert _transition(api, tid, "submit_review").json()["status"] == "in_review"
    done = _transition(api, tid, "approve", actor=reviewer, comment="LGTM")
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert done.json()["completed_at"] is not None
    assert done.json()["reviewer_agent_id"] == reviewer


def test_illegal_transition_returns_409(api: dict[str, Any]) -> None:
    tid = _task(api, _project(api))  # backlog
    r = _transition(api, tid, "approve")  # approve is illegal from backlog
    assert r.status_code == 409
    assert r.json()["type"] == "https://titan.dev/errors/invalid-transition"


def test_transition_unauthenticated_401(api: dict[str, Any]) -> None:
    tid = _task(api, _project(api))
    r = api["client"].post(f"/api/v1/tasks/{tid}:transition", json={"action": "ready"})
    assert r.status_code == 401


def test_assign_requires_task_assign_permission(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    agent = _agent(api)
    assert _transition(api, tid, "ready").status_code == 200
    # operator has task:transition but NOT task:assign.
    r = _transition(api, tid, "assign", actor=agent, token="operator")
    assert r.status_code == 403


def test_unknown_action_is_409_not_422(api: dict[str, Any]) -> None:
    tid = _task(api, _project(api))
    r = _transition(api, tid, "frobnicate")
    assert r.status_code == 409
    assert r.json()["type"] == "https://titan.dev/errors/invalid-transition"


def test_transition_writes_matching_message(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    agent = _agent(api)
    _transition(api, tid, "ready")
    _transition(api, tid, "assign", actor=agent, comment="you're up")

    msgs = (
        api["client"]
        .get(
            f"/api/v1/projects/{pid}/messages?kind=task_assignment",
            headers=api["token_for"]("owner"),
        )
        .json()
    )
    mine = [m for m in msgs["items"] if m["task_id"] == tid]
    assert mine and mine[0]["body"]["action"] == "assign"
    assert mine[0]["sender_agent_id"] == agent  # sender is the acting agent


# --- review & reopen (TITAN-BCK-016) ---------------------------------------


def test_assignee_cannot_self_review(api: dict[str, Any]) -> None:
    """DoD: a task where assignee == reviewer is rejected and cannot reach done."""
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    _drive_to_in_review(api, tid, assignee)

    self_review = _transition(api, tid, "approve", actor=assignee)  # same agent
    assert self_review.status_code == 409
    still = api["client"].get(f"/api/v1/tasks/{tid}", headers=api["token_for"]("owner")).json()
    assert still["status"] == "in_review"  # never reached done

    reviewer = _agent(api, "reviewer")
    ok = _transition(api, tid, "approve", actor=reviewer)
    assert ok.status_code == 200 and ok.json()["status"] == "done"


def test_reopen_requires_permission_and_creates_new_run(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, tid, assignee)
    assert _transition(api, tid, "approve", actor=reviewer).json()["status"] == "done"

    # maintainer's bundle lacks task:reopen.
    assert _transition(api, tid, "reopen", token="maintainer").status_code == 403

    reopened = _transition(api, tid, "reopen")  # owner has *
    assert reopened.status_code == 200 and reopened.json()["status"] == "reopened"
    runs = (
        api["client"].get(f"/api/v1/runs?task_id={tid}", headers=api["token_for"]("owner")).json()
    )
    assert len(runs["items"]) == 1 and runs["items"][0]["agent_id"] == assignee


def test_assign_unknown_agent_is_422_not_500(api: dict[str, Any]) -> None:
    """A well-formed but non-existent actor_agent_id is a 422, not an FK 500."""
    pid = _project(api)
    tid = _task(api, pid)
    assert _transition(api, tid, "ready").status_code == 200
    r = _transition(api, tid, "assign", actor=str(uuid.uuid4()))
    assert r.status_code == 422
    assert r.json()["type"] == "https://titan.dev/errors/validation"


def test_assign_agent_from_other_project_is_422(api: dict[str, Any]) -> None:
    """actor_agent_id must belong to the task's project (or be org-level)."""
    pid, other = _project(api), _project(api)
    tid = _task(api, pid)
    bound = (
        api["client"]
        .post(
            "/api/v1/agents",
            json={"role": "backend", "name": "foreign", "model_binding": _MB, "project_id": other},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    assert _transition(api, tid, "ready").status_code == 200
    r = _transition(api, tid, "assign", actor=bound)
    assert r.status_code == 422


def test_reopen_clears_completed_at(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, tid, assignee)
    assert _transition(api, tid, "approve", actor=reviewer).json()["completed_at"] is not None
    reopened = _transition(api, tid, "reopen")
    assert reopened.json()["status"] == "reopened" and reopened.json()["completed_at"] is None


def test_request_changes_then_resubmit(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, tid, assignee)
    cr = _transition(api, tid, "request_changes", actor=reviewer, comment="fix the tests")
    assert cr.json()["status"] == "changes_requested"
    assert _transition(api, tid, "resubmit").json()["status"] == "in_review"


# --- scheduler: dependencies (TITAN-BCK-014) -------------------------------


def test_ready_blocked_until_dependencies_done(api: dict[str, Any]) -> None:
    pid = _project(api)
    a, b = _task(api, pid), _task(api, pid)
    api["client"].post(
        f"/api/v1/tasks/{b}/dependencies",
        json={"depends_on_id": a},
        headers=api["token_for"]("owner"),
    )
    r = _transition(api, b, "ready")  # a is not done
    assert r.status_code == 409


def test_completing_blocker_readies_dependent_and_enqueues(api: dict[str, Any]) -> None:
    """DoD: completing the last blocker readies the dependent IN THE SAME
    transaction and enqueues it on stream:tasks.ready."""
    pid = _project(api)
    a, b = _task(api, pid), _task(api, pid)
    api["client"].post(
        f"/api/v1/tasks/{b}/dependencies",
        json={"depends_on_id": a},
        headers=api["token_for"]("owner"),
    )
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, a, assignee)
    done = _transition(api, a, "approve", actor=reviewer)  # a -> done
    assert done.json()["status"] == "done"

    # b was promoted backlog -> ready atomically with a's completion.
    b_now = api["client"].get(f"/api/v1/tasks/{b}", headers=api["token_for"]("owner")).json()
    assert b_now["status"] == "ready"

    async def enqueued_on_ready_stream() -> bool:
        import json as _json

        async with api["engine"].connect() as conn:
            rows = await conn.execute(
                text("SELECT payload FROM outbox WHERE topic = :t"), {"t": STREAM_TASKS_READY}
            )
            for (payload,) in rows:
                wrapper = _json.loads(payload) if isinstance(payload, str) else payload
                if wrapper.get("data", {}).get("task_id") == b:
                    return True
            return False

    assert run_async(enqueued_on_ready_stream) is True


# --- race-safe assignment (TITAN-BCK-015) ----------------------------------


def test_concurrent_claim_yields_exactly_one_winner(api: dict[str, Any]) -> None:
    """DoD: two workers racing for one task -> exactly one assigned winner,
    over 100 iterations."""
    from titan.db import uuid7
    from titan.orchestrator import service as orch

    pid = _project(api)
    a1, a2 = uuid.UUID(_agent(api)), uuid.UUID(_agent(api))
    engine = api["engine"]

    async def one_round() -> int:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        task_id = uuid7()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, project_id, title, status) "
                    "VALUES (:id, :pid, 'race', 'ready')"
                ),
                {"id": task_id, "pid": uuid.UUID(pid)},
            )
        first, second = await asyncio.gather(
            orch.claim_and_assign(engine, redis, task_id, a1),
            orch.claim_and_assign(engine, redis, task_id, a2),
        )
        await redis.aclose()
        return sum(1 for result in (first, second) if result is not None)

    for _ in range(100):
        assert run_async(one_round) == 1


def test_concurrent_sibling_completion_promotes_shared_dependent(api: dict[str, Any]) -> None:
    """Regression for the write-skew: C depends on A and B; approving A and B
    concurrently must still promote C to ready (the dependency-graph advisory
    lock serializes the two completions so neither misses the promotion)."""
    from titan.orchestrator import service as orch

    pid = _project(api)
    a, b, c = _task(api, pid), _task(api, pid), _task(api, pid)
    for blocker in (a, b):
        api["client"].post(
            f"/api/v1/tasks/{c}/dependencies",
            json={"depends_on_id": blocker},
            headers=api["token_for"]("owner"),
        )
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, a, assignee)
    _drive_to_in_review(api, b, assignee)
    engine = api["engine"]
    rid = uuid.UUID(reviewer)

    wm_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def approve(task_id: str) -> Any:
        async with engine.begin() as conn:
            task = await orch.get_task(conn, uuid.UUID(task_id))
            transition = orch.transitions.resolve(task.status, "approve")
            assert transition is not None
            return await orch.apply_transition(
                conn,
                task,
                transition,
                actor_agent_id=rid,
                actor_user_id=None,
                comment=None,
                # approve consults the TITAN-BRN-002 wm:{task}:events gate.
                redis=wm_redis,
            )

    async def race() -> Any:
        return await asyncio.gather(approve(a), approve(b))

    run_async(race)
    c_now = api["client"].get(f"/api/v1/tasks/{c}", headers=api["token_for"]("owner")).json()
    assert c_now["status"] == "ready"  # not stranded in backlog
