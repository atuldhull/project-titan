"""Phase 41 — the per-project delivery pipeline state machine (Vol 6 §2.1,
TITAN-WFL-001/002).

Covers the DoD: the 12-stage no-skip machine (an out-of-order transition is
rejected and logged, state unchanged); the CEO NOT_APPLICABLE skip with a logged
justification; iteration retries; a full walk to `released`; budget-hold pause +
resume; and — the headline acceptance — killing and restarting the orchestrator
(a fresh app on the same DB) resumes the project at the EXACT stage and iteration,
with the ledger reconstructing the full history.
"""

import datetime as dt
import json
import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from titan.events.envelope import Envelope
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.orchestrator import pipeline
from titan.realtime import channels
from titan.realtime.fanout import WsFanout

from .conftest import run_async

# ============================================================================
# Pure stage-machine unit tests (no DB, no I/O) — the no-skip invariant.
# ============================================================================


def test_twelve_ordered_stages() -> None:
    assert len(pipeline.STAGES) == 12
    assert pipeline.INITIAL_STAGE == "ceo_vision"
    assert pipeline.TERMINAL_STAGE == "handover"
    # Strictly increasing ordinals, no gaps.
    assert [pipeline.stage_index(s) for s in pipeline.STAGES] == list(range(12))


def test_next_stage_walks_then_terminates() -> None:
    assert pipeline.next_stage("ceo_vision") == "pm_backlog"
    assert pipeline.next_stage("ceo_release") == "handover"
    assert pipeline.next_stage("handover") is None
    assert pipeline.is_terminal("handover") and not pipeline.is_terminal("ceo_vision")


def test_resolve_advance_only_allows_immediate_successor() -> None:
    # Legal: exactly the next stage.
    ok = pipeline.resolve_advance("ceo_vision", "pm_backlog")
    assert ok is not None and ok.from_stage == "ceo_vision" and ok.to_stage == "pm_backlog"
    # Illegal: skip ahead, jump back, self-loop, or unknown stage — all None.
    assert pipeline.resolve_advance("ceo_vision", "architect_design") is None  # skip
    assert pipeline.resolve_advance("qa", "engineering") is None  # backward
    assert pipeline.resolve_advance("qa", "qa") is None  # self
    assert pipeline.resolve_advance("handover", "handover") is None  # terminal
    assert pipeline.resolve_advance("ceo_vision", "not_a_stage") is None  # unknown target


# ============================================================================
# WS fan-out unit tests (fakeredis) — pipeline + kanban frames reach the socket.
# ============================================================================

_PID = str(uuid.uuid4())


def _env(payload: dict[str, Any]) -> Envelope:
    return Envelope(
        event_id=str(uuid.uuid4()),
        topic=STREAM_EVENTS_DOMAIN,
        occurred_at=dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.UTC),
        actor="test",
        payload=payload,
    )


async def _collect(fanout: WsFanout, redis: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ps = redis.pubsub()
    await ps.psubscribe("ws.project.*")
    await ps.get_message(timeout=0.05)
    await fanout.handle_envelope(_env(payload))
    out: dict[str, Any] = {}
    while True:
        message = await ps.get_message(timeout=0.1)
        if message is None:
            break
        if message["type"] in ("message", "pmessage"):
            out[message["channel"]] = json.loads(message["data"])
    await ps.aclose()
    return out


def test_pipeline_event_fans_to_project_channel() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fanout = WsFanout(redis, org="titan")
        payload = {
            "type": "pipeline.updated",
            "pipeline": {"project_id": _PID, "current_stage": "pm_backlog", "iteration": 0},
            "change": {"event": "stage_complete", "stage": "ceo_vision"},
        }
        published = await _collect(fanout, redis, payload)
        frame = published[channels.project_channel(_PID)]
        assert frame["type"] == "pipeline.updated"
        assert frame["payload"]["pipeline"]["current_stage"] == "pm_backlog"
        assert frame["payload"]["change"]["event"] == "stage_complete"
        # Buffered for reconnect replay.
        assert await redis.llen(channels.buffer_key(_PID)) == 1
        await redis.aclose()

    run_async(scenario)


def test_kanban_event_fans_to_project_channel() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fanout = WsFanout(redis, org="titan")
        card = {"task_id": str(uuid.uuid4()), "project_id": _PID, "lane": "IN_LOOP"}
        published = await _collect(fanout, redis, {"type": "task.kanban", "kanban": card})
        frame = published[channels.project_channel(_PID)]
        assert frame["type"] == "task.kanban" and frame["payload"]["lane"] == "IN_LOOP"
        await redis.aclose()

    run_async(scenario)


def test_handoff_event_fans_to_project_channel() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fanout = WsFanout(redis, org="titan")
        handoff = {
            "project_id": _PID,
            "stage": "ceo_vision",
            "verdict": "fail",
            "reasons": ["x"],
            "attempt": 1,
        }
        published = await _collect(fanout, redis, {"type": "handoff.evaluated", "handoff": handoff})
        frame = published[channels.project_channel(_PID)]
        assert frame["type"] == "handoff.evaluated" and frame["payload"]["verdict"] == "fail"
        await redis.aclose()

    run_async(scenario)


# ============================================================================
# Integration tests (real schema + api fixture).
# ============================================================================


def _project(api: dict[str, Any], owner: str = "owner") -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"](owner),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _get_pipeline(api: dict[str, Any], pid: str, token: str = "owner") -> Any:
    return api["client"].get(f"/api/v1/projects/{pid}/pipeline", headers=api["token_for"](token))


def _advance(
    api: dict[str, Any], pid: str, target: str, *, token: str = "owner", note: str | None = None
) -> Any:
    body: dict[str, Any] = {"target_stage": target}
    if note is not None:
        body["note"] = note
    return api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:advance", json=body, headers=api["token_for"](token)
    )


@pytest.mark.schema
def test_pipeline_initializes_at_first_stage(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _get_pipeline(api, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pipeline"]["current_stage"] == "ceo_vision"
    assert body["pipeline"]["iteration"] == 0
    assert body["pipeline"]["status"] == "running"
    assert body["pipeline"]["current_stage_label"].startswith("CEO")
    assert body["stages"] == list(pipeline.STAGES)
    # The opening `enter` event is on the ledger.
    assert [(e["stage"], e["event"]) for e in body["history"]] == [("ceo_vision", "enter")]


@pytest.mark.schema
def test_advance_to_next_stage_records_history(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _advance(api, pid, "pm_backlog", note="vision done")
    assert r.status_code == 200, r.text
    assert r.json()["current_stage"] == "pm_backlog"
    assert r.json()["iteration"] == 0
    events = [(e["stage"], e["event"]) for e in _get_pipeline(api, pid).json()["history"]]
    assert events == [
        ("ceo_vision", "enter"),
        ("ceo_vision", "stage_complete"),
        ("pm_backlog", "enter"),
    ]


@pytest.mark.schema
def test_out_of_order_advance_rejected_and_state_unchanged(api: dict[str, Any]) -> None:
    """TITAN-WFL-002: a skip is a 409 with a logged rejected-transition event and
    NO state change."""
    pid = _project(api)
    r = _advance(api, pid, "architect_design")  # skips pm_backlog + pmgr_plan
    assert r.status_code == 409, r.text
    body = _get_pipeline(api, pid).json()
    # State unchanged — still at the first stage, iteration 0.
    assert body["pipeline"]["current_stage"] == "ceo_vision"
    assert body["pipeline"]["iteration"] == 0
    # The rejection is on the append-only ledger with the attempted target.
    rejected = [e for e in body["history"] if e["event"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["detail"]["target_stage"] == "architect_design"


@pytest.mark.schema
def test_not_applicable_skips_current_stage_with_justification(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:not-applicable",
        json={"target_stage": "pm_backlog", "justification": "docs-only change"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_stage"] == "pm_backlog"
    na = [e for e in _get_pipeline(api, pid).json()["history"] if e["event"] == "not_applicable"]
    assert len(na) == 1 and na[0]["detail"]["justification"] == "docs-only change"


@pytest.mark.schema
def test_not_applicable_still_cannot_skip_two_stages(api: dict[str, Any]) -> None:
    """The NOT_APPLICABLE exception advances ONLY to the immediate successor — it
    is not a licence to jump."""
    pid = _project(api)
    r = api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:not-applicable",
        json={"target_stage": "architect_design", "justification": "x"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 409, r.text
    assert _get_pipeline(api, pid).json()["pipeline"]["current_stage"] == "ceo_vision"


@pytest.mark.schema
def test_not_applicable_requires_override_permission(api: dict[str, Any]) -> None:
    """Operator (pipeline:advance only) can advance but NOT mark NOT_APPLICABLE."""
    pid = _project(api)
    # operator can advance…
    assert _advance(api, pid, "pm_backlog", token="operator").status_code == 200
    # …but the NOT_APPLICABLE override is 403 for it.
    r = api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:not-applicable",
        json={"target_stage": "pmgr_plan", "justification": "x"},
        headers=api["token_for"]("operator"),
    )
    assert r.status_code == 403, r.text


@pytest.mark.schema
def test_advance_requires_advance_permission(api: dict[str, Any]) -> None:
    pid = _project(api)
    # viewer holds only *:read → cannot advance.
    assert _advance(api, pid, "pm_backlog", token="viewer").status_code == 403


@pytest.mark.schema
def test_retry_bumps_iteration(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:retry",
        json={"reason": "gate failed"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["iteration"] == 1
    assert r.json()["current_stage"] == "ceo_vision"


@pytest.mark.schema
def test_restart_resumes_exact_stage_and_iteration(api: dict[str, Any]) -> None:
    """The headline DoD: kill + restart the orchestrator (a fresh app on the same
    DB) and the project resumes at the EXACT stage and iteration, with the ledger
    reconstructing the full history."""
    pid = _project(api)
    assert _advance(api, pid, "pm_backlog").status_code == 200
    assert _advance(api, pid, "pmgr_plan").status_code == 200
    # Two rework re-entries at pmgr_plan.
    for _ in range(2):
        api["client"].post(
            f"/api/v1/projects/{pid}/pipeline:retry", json={}, headers=api["token_for"]("owner")
        )

    # "Restart": a brand-new app instance over the same database (lifespan runs
    # on `with`, re-initializing app.state from scratch — a genuine cold start).
    with api["new_client"]() as fresh:
        r = fresh.get(f"/api/v1/projects/{pid}/pipeline", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pipeline"]["current_stage"] == "pmgr_plan"
    assert body["pipeline"]["iteration"] == 2
    # Full history survived the restart and replays in order.
    events = [(e["stage"], e["event"]) for e in body["history"]]
    assert events[:5] == [
        ("ceo_vision", "enter"),
        ("ceo_vision", "stage_complete"),
        ("pm_backlog", "enter"),
        ("pm_backlog", "stage_complete"),
        ("pmgr_plan", "enter"),
    ]
    assert events.count(("pmgr_plan", "enter")) == 3  # initial + 2 retries


@pytest.mark.schema
def test_full_walk_to_released(api: dict[str, Any]) -> None:
    pid = _project(api)
    for target in pipeline.STAGES[1:]:  # advance through every successor
        assert _advance(api, pid, target).status_code == 200, target
    body = _get_pipeline(api, pid).json()
    assert body["pipeline"]["current_stage"] == "handover"
    assert body["pipeline"]["status"] == "released"
    assert any(e["event"] == "released" for e in body["history"])
    # Advancing out of the terminal stage is rejected.
    assert _advance(api, pid, "handover").status_code == 409


@pytest.mark.schema
def test_budget_hold_pauses_pipeline_then_resumes(api: dict[str, Any]) -> None:
    """Point 4: at 100% budget the pipeline pauses; raising the budget resumes it
    exactly where it paused."""
    pid = _project(api)
    assert _advance(api, pid, "pm_backlog").status_code == 200  # now at pm_backlog

    async def _hold() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
                    "VALUES (:id, 'project', :pid, 'project budget reached', 100)"
                ),
                {"id": uuid.uuid4(), "pid": pid},
            )

    run_async(_hold)
    # Paused: advance is refused, state unchanged, and a budget_hold event logged.
    r = _advance(api, pid, "pmgr_plan")
    assert r.status_code == 409 and "budget" in r.json()["detail"].lower()
    body = _get_pipeline(api, pid).json()
    assert body["pipeline"]["current_stage"] == "pm_backlog"
    assert any(e["event"] == "budget_hold" for e in body["history"])

    # Raise the budget → resumes exactly where it paused (still pm_backlog).
    resume = api["client"].post(
        f"/api/v1/projects/{pid}/pipeline:resume-budget", headers=api["token_for"]("owner")
    )
    assert resume.status_code == 200, resume.text
    assert _advance(api, pid, "pmgr_plan").status_code == 200


@pytest.mark.schema
def test_pipeline_init_is_idempotent_single_enter(api: dict[str, Any]) -> None:
    """Repeated first-touch paths (two GETs + the reconciler) must NOT re-log the
    opening 'enter' — the append-only ledger carries exactly one (regression for
    the double-log race: only the caller that actually inserts logs the opening)."""
    pid = _project(api)
    _get_pipeline(api, pid)  # first touch — creates + logs one 'enter'
    _get_pipeline(api, pid)  # pure read — must not re-log

    async def _reconcile_then_count() -> int:
        from titan.orchestrator import service as orch

        async with api["engine"].begin() as conn:
            await orch.reconcile_pipelines(conn)  # project already has a pipeline → no-op
            rows = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pipeline_stage_events "
                        "WHERE project_id = :p AND event = 'enter' AND stage = 'ceo_vision'"
                    ),
                    {"p": uuid.UUID(pid)},
                )
            ).scalar_one()
        return int(rows)

    assert run_async(_reconcile_then_count) == 1


@pytest.mark.schema
def test_stage_ledger_is_append_only(api: dict[str, Any]) -> None:
    """TITAN-AGT-019 / TITAN-WFL-004 foundation: the stage audit cannot be rewritten."""
    pid = _project(api)
    _get_pipeline(api, pid)  # seed one ledger row

    async def _mutate() -> str:
        try:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE pipeline_stage_events SET event = 'tampered' WHERE project_id = :p"
                    ),
                    {"p": pid},
                )
            return "no-error"
        except Exception as exc:  # noqa: BLE001 — asserting the trigger fires
            return type(exc).__name__

    assert run_async(_mutate) != "no-error"
