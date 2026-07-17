"""Phase 12 integration tests: runs + artifacts (§8.2) and the Storage seam.

Happy path + auth failure + validation failure per endpoint, the :cancel state
machine, object-level scoping through the task's project, and the filesystem
Storage round-trip. Needs the disposable test DB (schema marker) + fakeredis."""

import uuid
from typing import Any

import pytest

from tests.conftest import run_async

pytestmark = pytest.mark.schema

_MODEL_BINDING = {"provider": "ollama", "model": "qwen2.5-coder:14b"}


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _task(api: dict[str, Any], project_id: str) -> str:
    resp = api["client"].post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": "do work"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _agent(api: dict[str, Any]) -> str:
    resp = api["client"].post(
        "/api/v1/agents",
        json={"role": "backend", "name": "worker", "model_binding": _MODEL_BINDING},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _create_run(api: dict[str, Any], task_id: str, agent_id: str, token: str = "owner") -> Any:
    return api["client"].post(
        f"/api/v1/tasks/{task_id}/runs",
        json={"agent_id": agent_id, "input": {"prompt_ref": "ctx-1"}},
        headers=api["token_for"](token),
    )


def test_create_run_201_attempt_increments(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)

    first = _create_run(api, tid, aid)
    assert first.status_code == 201, first.text
    assert first.json()["attempt"] == 1 and first.json()["status"] == "queued"

    # While attempt 1 is still ACTIVE, a second run is refused — one owner per
    # task at any time (TITAN-AGT-012, Phase 32).
    blocked = _create_run(api, tid, aid)
    assert blocked.status_code == 409, blocked.text

    # Once attempt 1 is terminal, the retry attempt is legal and increments.
    cancel = api["client"].post(
        f"/api/v1/runs/{first.json()['id']}:cancel", headers=api["token_for"]("owner")
    )
    assert cancel.status_code == 200, cancel.text
    second = _create_run(api, tid, aid)
    assert second.status_code == 201, second.text
    assert second.json()["attempt"] == 2


def test_create_run_requires_run_create(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    # maintainer's bundle lacks run:create.
    resp = _create_run(api, tid, aid, token="maintainer")
    assert resp.status_code == 403


def test_create_run_unknown_agent_422(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    resp = _create_run(api, tid, str(uuid.uuid4()))
    assert resp.status_code == 422


def test_create_run_unauthenticated_401(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    resp = api["client"].post(f"/api/v1/tasks/{tid}/runs", json={"agent_id": aid, "input": {}})
    assert resp.status_code == 401


def test_get_run_and_list_by_task(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]
    client, headers = api["client"], api["token_for"]("viewer")  # *:read

    got = client.get(f"/api/v1/runs/{rid}", headers=headers)
    assert got.status_code == 200 and got.json()["id"] == rid

    listed = client.get(f"/api/v1/runs?task_id={tid}", headers=headers)
    assert listed.status_code == 200
    assert rid in {r["id"] for r in listed.json()["items"]}


def test_get_run_404(api: dict[str, Any]) -> None:
    resp = api["client"].get(f"/api/v1/runs/{uuid.uuid4()}", headers=api["token_for"]("owner"))
    assert resp.status_code == 404


def test_cancel_run_state_machine(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]
    client, headers = api["client"], api["token_for"]("owner")

    cancelled = client.post(f"/api/v1/runs/{rid}:cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["finished_at"] is not None

    # Cancelling a terminal run → 409.
    again = client.post(f"/api/v1/runs/{rid}:cancel", headers=headers)
    assert again.status_code == 409
    assert again.json()["type"] == "https://titan.dev/errors/conflict"


def test_cancel_run_requires_run_cancel(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]
    # operator has run:create but not run:cancel.
    resp = api["client"].post(f"/api/v1/runs/{rid}:cancel", headers=api["token_for"]("operator"))
    assert resp.status_code == 403


def test_run_object_scope_hides_out_of_project(api: dict[str, Any]) -> None:
    """A run in a project the caller can't see is 404 (object-level authz via
    the task's project)."""
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]

    other = _project(api)  # scoped user gets a role only on THIS project
    api["grant_project_role"]("scoped", other, "maintainer")
    headers = api["token_for"]("scoped", roles=[])
    assert api["client"].get(f"/api/v1/runs/{rid}", headers=headers).status_code == 404


def test_artifacts_via_storage_round_trip(api: dict[str, Any], tmp_path: Any) -> None:
    """record_artifact persists bytes through the filesystem Storage and the
    metadata is served by GET /runs/{id}/artifacts; the bytes read back intact."""
    from titan.runs.service import record_artifact
    from titan.runs.storage import FilesystemStorage

    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]

    storage = FilesystemStorage(tmp_path)
    payload = b"diff --git a/x b/x\n+hello\n"

    async def record() -> Any:
        async with api["engine"].begin() as conn:
            art = await record_artifact(
                conn,
                storage,
                run_id=uuid.UUID(rid),
                project_id=uuid.UUID(pid),
                kind="code_patch",
                data=payload,
                path="x.py",
            )
        # Storage round-trips the exact bytes behind the opaque ref.
        assert await storage.get(art.storage_ref) == payload
        return art

    art = run_async(record)
    assert art.sha256 and art.size_bytes == len(payload)
    assert art.storage_ref.startswith("fs:")  # opaque, not a raw path

    listed = api["client"].get(f"/api/v1/runs/{rid}/artifacts", headers=api["token_for"]("owner"))
    assert listed.status_code == 200
    items = listed.json()["items"]  # §4.1 list envelope, like every other list
    assert len(items) == 1 and items[0]["kind"] == "code_patch" and items[0]["path"] == "x.py"


def test_list_artifacts_empty_envelope(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    rid = _create_run(api, tid, aid).json()["id"]
    resp = api["client"].get(f"/api/v1/runs/{rid}/artifacts", headers=api["token_for"]("owner"))
    assert resp.status_code == 200 and resp.json() == {"items": [], "next_cursor": None}


def test_duplicate_attempt_rejected_by_unique_constraint(api: dict[str, Any]) -> None:
    """The uq_runs_task_id_attempt backstop (migration 0006) makes a duplicate
    (task_id, attempt) impossible even if a write path forgets the advisory
    lock — the durable guarantee behind the §8.2 attempt = max+1 invariant."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from titan.db import uuid7

    pid, aid = _project(api), _agent(api)
    tid = _task(api, pid)
    _create_run(api, tid, aid)  # attempt 1 now exists

    async def insert_dup() -> bool:
        try:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                        "VALUES (:id, :tid, :aid, 1, '{}'::jsonb)"
                    ),
                    {"id": uuid7(), "tid": tid, "aid": aid},
                )
            return False
        except IntegrityError:
            return True

    assert run_async(insert_dup) is True


# --- the project-scoped run collection (Phase 61; Vol 4 §6.5) ---------------
#
# `GET /runs` requires a task_id, which answers "what happened to this task".
# Nothing answered "what is this agent working on" — and that question can only
# be answered by runs, because `agents.current_task_id` has no writer anywhere
# and `agents.status` only ever holds idle|paused. These cover the seam the
# agent grid reads.


def test_project_runs_lists_every_run_in_the_project(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    first, second = _task(api, pid), _task(api, pid)
    _create_run(api, first, aid)
    _create_run(api, second, aid)

    resp = api["client"].get(f"/api/v1/projects/{pid}/runs", headers=api["token_for"]("owner"))
    assert resp.status_code == 200, resp.text
    tasks_seen = {r["task_id"] for r in resp.json()["items"]}
    assert tasks_seen == {first, second}


def test_project_runs_filters_by_agent(api: dict[str, Any]) -> None:
    """The query the grid actually makes: this agent's work, nobody else's."""
    pid = _project(api)
    mine, theirs = _agent(api), _agent(api)
    my_task, their_task = _task(api, pid), _task(api, pid)
    _create_run(api, my_task, mine)
    _create_run(api, their_task, theirs)

    resp = api["client"].get(
        f"/api/v1/projects/{pid}/runs",
        params={"agent_id": mine},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [r["task_id"] for r in items] == [my_task]
    assert {r["agent_id"] for r in items} == {mine}


def test_project_runs_filters_by_status(api: dict[str, Any]) -> None:
    pid, aid = _project(api), _agent(api)
    live, done = _task(api, pid), _task(api, pid)
    _create_run(api, live, aid)
    cancelled = _create_run(api, done, aid)
    api["client"].post(
        f"/api/v1/runs/{cancelled.json()['id']}:cancel", headers=api["token_for"]("owner")
    )

    resp = api["client"].get(
        f"/api/v1/projects/{pid}/runs",
        params={"status": "queued"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    assert [r["task_id"] for r in resp.json()["items"]] == [live]


def test_project_runs_never_leaks_another_projects_runs(api: dict[str, Any]) -> None:
    """The reason this route is project-scoped rather than a bare ?agent_id=.

    Agents are org-level and can work across projects, but a run authorizes
    through its project (TITAN-SEC-032). One agent, two projects: asking project
    A must not surface the run it did in project B.
    """
    a_pid, b_pid = _project(api), _project(api)
    shared = _agent(api)
    a_task, b_task = _task(api, a_pid), _task(api, b_pid)
    _create_run(api, a_task, shared)
    _create_run(api, b_task, shared)

    resp = api["client"].get(
        f"/api/v1/projects/{a_pid}/runs",
        params={"agent_id": shared},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    assert [r["task_id"] for r in resp.json()["items"]] == [a_task]


def test_project_runs_unknown_project_404_not_empty_page(api: dict[str, Any]) -> None:
    resp = api["client"].get(
        f"/api/v1/projects/{uuid.uuid4()}/runs", headers=api["token_for"]("owner")
    )
    assert resp.status_code == 404, resp.text


def test_project_runs_requires_read_in_project(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].get(f"/api/v1/projects/{pid}/runs")
    assert resp.status_code == 401


def test_project_runs_paginates_without_ambiguous_columns(api: dict[str, Any]) -> None:
    """Guards the subquery-not-JOIN decision in `list_runs_by_project`.

    Keyset splices bare `created_at`/`id` into ORDER BY and its seek predicate.
    `tasks` has columns of both names, so a JOIN would make them ambiguous and
    Postgres would reject the statement — and the seek clause only appears on
    the SECOND page, so page one would pass while paging blew up. This walks the
    cursor to prove both pages execute.
    """
    pid, aid = _project(api), _agent(api)
    for _ in range(3):
        _create_run(api, _task(api, pid), aid)

    first = api["client"].get(
        f"/api/v1/projects/{pid}/runs",
        params={"limit": 2},
        headers=api["token_for"]("owner"),
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 2
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    second = api["client"].get(
        f"/api/v1/projects/{pid}/runs",
        params={"limit": 2, "cursor": cursor},
        headers=api["token_for"]("owner"),
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    # No row repeats across the page boundary.
    seen = [r["id"] for r in first.json()["items"]] + [r["id"] for r in second.json()["items"]]
    assert len(set(seen)) == 3
