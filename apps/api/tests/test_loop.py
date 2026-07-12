"""Phase 44 — the self-improvement loop engine + reviewer isolation
(Vol 6 §3, TITAN-WFL-010–014).

Driven by a scripted mock LLM (the ``ScriptedDriver``): a converging task exits
after 2 cycles with a distinct convergence record; a non-converging task records
exactly 3 cycles then ESCALATED (never a 4th); a task with no loop record cannot
MERGE; a twice-refuted upheld finding and an A→B→A oscillation route to Architect
arbitration through the Phase-31 conflict machine; and the reviewer's audit log
contains no implementation-side chain-of-thought.
"""

import uuid
from collections.abc import Sequence
from typing import Any, cast

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.loop import engine
from titan.loop import service as loop
from titan.loop.engine import (
    CONVERGENCE_STREAK,
    CycleTally,
    Disposition,
    Finding,
    OscillationTracker,
    ReviewerContext,
    next_consecutive_clean,
)

pytestmark = pytest.mark.schema

_MB = {"provider": "anthropic", "model": "claude-x", "temperature": 0.0}


# --- the scripted mock LLM driver -------------------------------------------


class ScriptedDriver:
    """A deterministic :class:`~titan.loop.engine.LoopDriver` double. ``findings``
    maps a cycle number to the reviewer's findings; ``dispositions`` maps a finding
    fingerprint to the engineer's response; ``upheld`` is the reviewer's re-judgment
    of a refutation. Records every :class:`ReviewerContext` it received so a test can
    prove the reviewer never saw implementer context."""

    def __init__(
        self,
        *,
        findings: dict[int, Sequence[Finding]] | None = None,
        dispositions: dict[str, Disposition] | None = None,
        upheld: bool = True,
        stages_pass: bool = True,
    ) -> None:
        self.findings = findings or {}
        self.dispositions = dispositions or {}
        self.upheld = upheld
        self.stages_pass = stages_pass
        self.reviewer_contexts: list[ReviewerContext] = []

    async def review(self, ctx: ReviewerContext, cycle: int) -> Sequence[Finding]:
        self.reviewer_contexts.append(ctx)
        return self.findings.get(cycle, [])

    async def dispose(self, finding: Finding, cycle: int) -> Disposition:
        return self.dispositions.get(finding.fingerprint, Disposition(kind="fixed", reason="fixed"))

    async def rejudge(self, finding: Finding, disposition: Disposition, cycle: int) -> bool:
        return self.upheld

    async def stage_report(self, stage: str, cycle: int) -> tuple[bool, str]:
        return self.stages_pass, "ok"


def _finding(fp: str, *, severity: str = "major", hunk: str = "") -> Finding:
    return Finding(
        fingerprint=fp,
        severity=severity,
        file="app.py",
        line=10,
        rationale=f"issue {fp}",
        suggestion="fix it",
        hunk_key=hunk,
    )


# --- setup helpers ----------------------------------------------------------


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
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


def _task_in_progress(api: dict[str, Any], pid: str) -> str:
    r = api["client"].post(
        f"/api/v1/projects/{pid}/tasks", json={"title": "work"}, headers=api["token_for"]("owner")
    )
    assert r.status_code == 201, r.text
    tid = str(r.json()["id"])
    agent = api["client"].post(
        "/api/v1/agents",
        json={"role": "backend", "name": "backend-bot", "model_binding": _MB},
        headers=api["token_for"]("owner"),
    )
    aid = str(agent.json()["id"])
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=aid).status_code == 200
    assert _transition(api, tid, "start").json()["status"] == "in_progress"
    return tid


def _run(api: dict[str, Any], tid: str, driver: ScriptedDriver, **kw: Any) -> loop.LoopOutcome:
    async def _do() -> loop.LoopOutcome:
        return await loop.run_loop(
            api["engine"],
            uuid.UUID(tid),
            driver,
            actor_user_id=api["user_ids"]["owner"],
            pr_diff="- old\n+ new",
            task_spec="Implement the widget",
            architecture_excerpt="Component: WidgetService (use_cases)",
            **kw,
        )

    return cast(loop.LoopOutcome, run_async(_do))


def _loop_view(api: dict[str, Any], tid: str) -> dict[str, Any]:
    r = api["client"].get(f"/api/v1/tasks/{tid}/loop", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    return cast(dict[str, Any], r.json())


# --- pure engine (hermetic) -------------------------------------------------


def test_cycle_is_clean_only_when_no_new_and_no_open() -> None:
    assert CycleTally(new_minor_plus=0, open_minor_plus=0).clean
    assert not CycleTally(new_minor_plus=1, open_minor_plus=0).clean
    assert not CycleTally(new_minor_plus=0, open_minor_plus=1).clean


def test_convergence_counter() -> None:
    n = 0
    n = next_consecutive_clean(n, CycleTally(0, 0))
    assert n == 1
    n = next_consecutive_clean(n, CycleTally(1, 0))  # a dirty cycle resets it
    assert n == 0
    n = next_consecutive_clean(n, CycleTally(0, 0))
    n = next_consecutive_clean(n, CycleTally(0, 0))
    assert n == CONVERGENCE_STREAK


def test_oscillation_tracker_detects_A_B_A() -> None:
    t = OscillationTracker()
    assert not t.record("h", "A")
    assert not t.record("h", "B")
    assert t.record("h", "A")  # A re-appears after B → oscillation
    # A different hunk is independent.
    assert not t.record("other", "A")


def test_severity_threshold() -> None:
    assert not engine.is_minor_plus(_finding("f", severity="info"))
    assert engine.is_minor_plus(_finding("f", severity="minor"))
    assert engine.is_minor_plus(_finding("f", severity="blocking"))


# --- DoD: convergence + exhaustion ------------------------------------------


def test_converging_task_exits_after_two_cycles(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    driver = ScriptedDriver(findings={})  # reviewer finds nothing, ever
    outcome = _run(api, tid, driver)
    assert outcome.status == "converged"
    assert outcome.termination_reason == "converged"  # distinctly logged, not exhaustion
    assert outcome.cycles == CONVERGENCE_STREAK == 2
    assert outcome.task_status == "merged"
    view = _loop_view(api, tid)
    assert len(view["cycles"]) == 2 and view["cycles"][-1]["outcome"] == "converged"


def test_non_converging_task_exhausts_at_three_and_escalates(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    # A distinct new finding every cycle (each fixed) — never two clean in a row.
    driver = ScriptedDriver(findings={1: [_finding("a")], 2: [_finding("b")], 3: [_finding("c")]})
    outcome = _run(api, tid, driver)
    assert outcome.status == "escalated"
    assert outcome.termination_reason == "exhausted"
    assert outcome.cycles == 3
    assert outcome.task_status == "escalated"
    # No task ever records a 4th cycle.
    view = _loop_view(api, tid)
    assert len(view["cycles"]) == 3
    assert [c["cycle_number"] for c in view["cycles"]] == [1, 2, 3]
    # Exhaustion routes to the Architect via the Phase-31 conflict machine.
    assert _arbitration_count(api, pid) == 1


def test_max_cycles_is_never_exceeded_with_higher_cap(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    driver = ScriptedDriver(findings={i: [_finding(f"f{i}")] for i in range(1, 10)})
    outcome = _run(api, tid, driver, max_cycles=4)
    assert outcome.cycles == 4 and outcome.status == "escalated"
    assert len(_loop_view(api, tid)["cycles"]) == 4  # exactly the cap, never a 5th


# --- DoD: the TITAN-WFL-010 merge gate --------------------------------------


def test_merge_without_loop_record_is_rejected(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    assert _transition(api, tid, "enter_loop").json()["status"] == "in_loop"
    # No loop record → merge is technically impossible (TITAN-WFL-010).
    r = _transition(api, tid, "merge")
    assert r.status_code == 409 and "loop" in r.json()["detail"].lower()


# --- DoD: refute-or-fix arbitration + oscillation ---------------------------


def _arbitration_count(api: dict[str, Any], pid: str) -> int:
    async def _count() -> int:
        async with api["engine"].connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM conflicts WHERE project_id = :p "
                            "AND kind = 'technical' AND state = 'arbitration' "
                            "AND arbiter = 'architect'"
                        ),
                        {"p": uuid.UUID(pid)},
                    )
                ).scalar_one()
            )

    return cast(int, run_async(_count))


def test_twice_refuted_upheld_finding_routes_to_arbitration(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    # The reviewer re-asserts the same finding; the engineer refutes it each cycle
    # and the reviewer upholds it — the second upheld refutation escalates.
    fp = "disputed"
    driver = ScriptedDriver(
        findings={1: [_finding(fp)], 2: [_finding(fp)], 3: [_finding(fp)]},
        dispositions={fp: Disposition(kind="refuted", reason="works as intended")},
        upheld=True,
    )
    outcome = _run(api, tid, driver)
    assert outcome.status == "arbitration"
    assert outcome.cycles == 2  # escalates on the 2nd upheld refutation
    assert outcome.task_status == "in_loop"  # stays in loop awaiting the verdict
    assert _arbitration_count(api, pid) == 1
    view = _loop_view(api, tid)
    disputed = next(f for f in view["findings"] if f["rationale"] == f"issue {fp}")
    assert disputed["disposition"] == "arbitration" and disputed["conflict_id"] is not None
    assert disputed["refute_upheld_count"] == 2


def test_oscillation_short_circuits_to_arbitration(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    # Distinct findings on the SAME hunk; the engineer edits it A → B → A.
    driver = ScriptedDriver(
        findings={
            1: [_finding("a", hunk="H")],
            2: [_finding("b", hunk="H")],
            3: [_finding("c", hunk="H")],
        },
        dispositions={
            "a": Disposition(kind="fixed", reason="A", new_hunk_state="A"),
            "b": Disposition(kind="fixed", reason="B", new_hunk_state="B"),
            "c": Disposition(kind="fixed", reason="A again", new_hunk_state="A"),
        },
    )
    outcome = _run(api, tid, driver, max_cycles=5)
    assert outcome.status == "arbitration"
    assert outcome.cycles == 3  # short-circuits, never burning cycles 4–5
    assert _arbitration_count(api, pid) == 1


# --- DoD: reviewer isolation (TITAN-WFL-011) --------------------------------


def test_reviewer_context_excludes_implementer_chain_of_thought(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    driver = ScriptedDriver(findings={})
    _run(api, tid, driver)

    # By construction, the reviewer only ever received a ReviewerContext (4 fields,
    # no implementer chain-of-thought field exists).
    assert driver.reviewer_contexts
    for ctx in driver.reviewer_contexts:
        assert set(vars(ctx)) == {
            "pr_diff",
            "task_spec",
            "architecture_excerpt",
            "constitution_version",
            "constitution_body",
        }

    # The persisted audit trail records the isolated-context manifest and no
    # implementation-side content.
    view = _loop_view(api, tid)
    reviews = [e for e in view["stage_events"] if e["stage"] == "review"]
    assert reviews
    for ev in reviews:
        assert ev["actor"] == "reviewer"
        assert ev["detail"]["context_sections"] == [
            "pr_diff",
            "task_spec",
            "architecture_excerpt",
            "constitution",
        ]
        assert ev["detail"]["excludes_implementer_context"] is True
        assert "implementer_notes" not in ev["detail"]


def test_loop_run_is_idempotent(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    driver = ScriptedDriver(findings={})
    first = _run(api, tid, driver)
    second = _run(api, tid, driver)  # re-invoking a terminal loop is a no-op read
    assert first.loop_id == second.loop_id
    assert second.status == "converged" and second.cycles == 2
    assert len(_loop_view(api, tid)["cycles"]) == 2  # no extra cycles recorded


# --- review-fix regressions -------------------------------------------------


def test_out_of_enum_severity_keeps_the_cycle_dirty(api: dict[str, Any]) -> None:
    """Review F1: an out-of-enum severity ('critical') must count as ≥minor in BOTH
    the new-finding tally and the open-findings count — otherwise an upheld critical
    finding is invisible and the loop wrongly converges (fail-open merge)."""
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    # Cycle 1 raises a 'critical' finding, refuted-and-upheld (count 1, still open);
    # cycles 2–3 raise nothing. The open critical must keep every cycle dirty.
    driver = ScriptedDriver(
        findings={1: [_finding("crit", severity="critical")]},
        dispositions={"crit": Disposition(kind="refuted", reason="nah")},
        upheld=True,
    )
    outcome = _run(api, tid, driver)
    assert outcome.status == "escalated"  # NOT converged/merged
    assert outcome.task_status != "merged"


def test_run_loop_resumes_after_a_mid_loop_crash(api: dict[str, Any]) -> None:
    """Review F2: a loop with a committed cycle from a crashed run must RESUME (not
    re-run cycle 1 and collide on the loop_cycles unique constraint)."""
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    assert _transition(api, tid, "enter_loop").json()["status"] == "in_loop"
    lid = uuid.uuid4()

    async def _seed_partial() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO loop_records (id, task_id, project_id, status, iteration, "
                    "cycle_count, max_cycles) VALUES (:id, :t, :p, 'running', 1, 1, 3)"
                ),
                {"id": lid, "t": uuid.UUID(tid), "p": uuid.UUID(pid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO loop_cycles (id, loop_id, cycle_number, new_findings_count, "
                    "clean, outcome) VALUES (:id, :l, 1, 1, false, 'continue')"
                ),
                {"id": uuid.uuid4(), "l": lid},
            )

    run_async(_seed_partial)
    # Resume with a converging driver: it continues at cycle 2 and converges at 3.
    outcome = _run(api, tid, ScriptedDriver(findings={}))
    assert outcome.loop_id == lid  # resumed the SAME loop, not a new one
    assert outcome.status == "converged" and outcome.cycles == 3
    assert [c["cycle_number"] for c in _loop_view(api, tid)["cycles"]] == [1, 2, 3]


def _seed_reflection(api: dict[str, Any], pid: str, tid: str) -> None:
    keys = ("goal", "what_worked", "what_surprised", "do_differently", "org_should_remember")
    metadata = {"record_subtype": "reflection", **{k: "x" for k in keys}}
    import json as _json

    async def _do() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_records (id, kind, scope, project_id, source_agent, "
                    "source_task_id, title, body_md, metadata, importance) VALUES "
                    "(:id, 'episodic', :scope, :pid, 'a', :tid, 't', 'b', CAST(:md AS jsonb), 0.5)"
                ),
                {
                    "id": uuid.uuid4(),
                    "scope": f"project:{pid}",
                    "pid": pid,
                    "tid": tid,
                    "md": _json.dumps(metadata),
                },
            )

    run_async(_do)


def test_reopen_supersedes_the_loop_forcing_a_fresh_one(api: dict[str, Any]) -> None:
    """Review F3: a reopened task cannot merge on its stale converged loop — the
    prior loop is superseded and a fresh loop must converge for the re-worked code."""
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    assert _run(api, tid, ScriptedDriver(findings={})).task_status == "merged"
    _seed_reflection(api, pid, tid)
    assert _transition(api, tid, "complete").json()["status"] == "done"
    assert _transition(api, tid, "reopen").json()["status"] == "reopened"
    # The prior converged loop is now superseded (visible on the latest record).
    assert _loop_view(api, tid)["record"]["status"] == "superseded"
    assert _transition(api, tid, "start").json()["status"] == "in_progress"
    assert _transition(api, tid, "enter_loop").json()["status"] == "in_loop"
    # A direct merge on the stale loop is blocked (TITAN-WFL-010 across iterations).
    assert _transition(api, tid, "merge").status_code == 409
    # A fresh loop (iteration 2) converges and now merge is allowed.
    out = _run(api, tid, ScriptedDriver(findings={}))
    assert out.status == "converged" and out.task_status == "merged"
    assert _loop_view(api, tid)["record"]["iteration"] == 2


def test_two_findings_in_one_cycle_both_reach_arbitration(api: dict[str, Any]) -> None:
    """Review F4: two arbitration-worthy findings in the same cycle are BOTH routed
    to the Architect — not just the last one."""
    pid = _project(api)
    tid = _task_in_progress(api, pid)
    driver = ScriptedDriver(
        findings={1: [_finding("x"), _finding("y")], 2: [_finding("x"), _finding("y")]},
        dispositions={
            "x": Disposition(kind="refuted", reason="x-wai"),
            "y": Disposition(kind="refuted", reason="y-wai"),
        },
        upheld=True,
    )
    outcome = _run(api, tid, driver)
    assert outcome.status == "arbitration"
    assert _arbitration_count(api, pid) == 2  # one conflict per disputed finding
    arb = [f for f in _loop_view(api, tid)["findings"] if f["disposition"] == "arbitration"]
    assert {f["rationale"] for f in arb} == {"issue x", "issue y"}
    assert all(f["conflict_id"] is not None for f in arb)
