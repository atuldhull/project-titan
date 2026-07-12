"""Phase 43 — the CEO clarification engine (Vol 6 §2.4, TITAN-WFL-007/008).

Two layers: the pure policy (resolve_policy — ≤5 essential questions, canonical /
inferable unknowns become assumptions) tested hermetically, and the persisted
engine (submit / answer / non-blocking timeout) plus the G1 intake rejection tested
over the real schema.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.clarification import service as clarification
from titan.clarification.engine import (
    MAX_CLARIFICATION_QUESTIONS,
    ProposedAssumption,
    ProposedQuestion,
    resolve_policy,
    timeout_assumptions,
)

pytestmark = pytest.mark.schema


# --- the pure policy (TITAN-WFL-007) ---------------------------------------


def test_policy_keeps_essential_questions() -> None:
    plan = resolve_policy(
        proposed_questions=[ProposedQuestion("content licensing", "Real licensing or mocked?")],
    )
    assert len(plan.questions) == 1
    assert plan.questions[0].topic == "content licensing"


def test_policy_converts_canonical_default_to_assumption() -> None:
    plan = resolve_policy(
        proposed_questions=[ProposedQuestion("which database", "Which database should we use?")],
    )
    # Asking a canonical-stack question is a policy violation — it is not asked.
    assert plan.questions == ()
    assert any(a.source == "canonical_default" for a in plan.assumptions)


def test_policy_converts_inferable_to_assumption() -> None:
    plan = resolve_policy(
        proposed_questions=[ProposedQuestion("region", "Which region?")],
        inferable_topics=frozenset({"region"}),
    )
    assert plan.questions == ()
    assert any(a.source == "org_memory" for a in plan.assumptions)


def test_policy_caps_at_five_and_defers_excess() -> None:
    proposed = [ProposedQuestion(f"topic{i}", f"q{i}?") for i in range(8)]
    plan = resolve_policy(proposed_questions=proposed)
    assert len(plan.questions) == MAX_CLARIFICATION_QUESTIONS
    excess = [a for a in plan.assumptions if a.source == "excess_question"]
    assert len(excess) == 3 and all(a.high_visibility for a in excess)


def test_policy_collapses_duplicate_topics() -> None:
    plan = resolve_policy(
        proposed_questions=[
            ProposedQuestion("scope", "How big?"),
            ProposedQuestion("Scope", "What scale?"),
        ]
    )
    assert len(plan.questions) == 1


def test_policy_carries_proposed_assumptions() -> None:
    plan = resolve_policy(
        proposed_questions=[],
        proposed_assumptions=[ProposedAssumption("payments are mocked")],
    )
    assert any(
        a.statement == "payments are mocked" and a.source == "proposed" for a in plan.assumptions
    )


def test_timeout_assumptions_are_high_visibility() -> None:
    assumptions = timeout_assumptions(["Real payments?"])
    assert len(assumptions) == 1
    assert assumptions[0].high_visibility and assumptions[0].source == "timeout_best_judgment"


# --- the persisted engine over the real schema ------------------------------


def _project(api: dict[str, Any], brief: str = "Build a CRM", owner: str = "owner") -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": brief},
        headers=api["token_for"](owner),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _open(api: dict[str, Any], pid: str, body: dict[str, Any], token: str = "owner") -> Any:
    return api["client"].post(
        f"/api/v1/projects/{pid}/clarifications", json=body, headers=api["token_for"](token)
    )


def test_submit_opens_batch_with_capped_questions_and_assumptions(api: dict[str, Any]) -> None:
    pid = _project(api)
    body = {
        "raw_text": "Build me a Netflix clone",
        "proposed_questions": (
            [{"topic": "which database", "question": "Which database?"}]  # canonical → assumption
            + [{"topic": f"t{i}", "question": f"q{i}?"} for i in range(7)]  # 7 essential, 2 excess
        ),
        "proposed_assumptions": [{"statement": "single region deployment"}],
    }
    r = _open(api, pid, body)
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["status"] == "open"
    assert len(batch["questions"]) == MAX_CLARIFICATION_QUESTIONS  # ≤5 (TITAN-WFL-007)
    sources = {a["source"] for a in batch["assumptions"]}
    assert "canonical_default" in sources and "excess_question" in sources and "proposed" in sources


def test_submit_with_no_questions_resolves_immediately(api: dict[str, Any]) -> None:
    """A request with nothing essential to ask must not linger open until the 24h
    deadline (where it would be mislabeled a best-judgment timeout) — it resolves
    at once with the CEO's logged assumptions."""
    pid = _project(api)
    r = _open(
        api, pid, {"raw_text": "req", "proposed_assumptions": [{"statement": "single region"}]}
    )
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["questions"] == []
    assert batch["status"] == "answered" and batch["resolution"] == "no_questions"
    assert any(a["source"] == "proposed" for a in batch["assumptions"])


def test_submit_rejects_duplicate_of_active_project(api: dict[str, Any]) -> None:
    _project(api, brief="Build a unique thing X")
    pid2 = _project(api, brief="another")
    r = _open(api, pid2, {"raw_text": "Build a unique thing X"})  # duplicate of an active project
    assert r.status_code == 409 and "duplicate" in r.json()["detail"].lower()


def test_submit_rejects_empty_request(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _open(api, pid, {"raw_text": "   "})
    assert r.status_code == 400 and "empty" in r.json()["detail"].lower()


def test_submit_is_single_batch_per_project(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _open(api, pid, {"raw_text": "req"}).status_code == 200
    assert _open(api, pid, {"raw_text": "req again"}).status_code == 409


def test_answer_closes_the_batch(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _open(
        api,
        pid,
        {"raw_text": "req", "proposed_questions": [{"topic": "scope", "question": "How big?"}]},
    )
    qid = r.json()["questions"][0]["id"]
    ans = api["client"].post(
        f"/api/v1/projects/{pid}/clarifications/answers",
        json={"answers": [{"question_id": qid, "answer": "enterprise scale"}]},
        headers=api["token_for"]("owner"),
    )
    assert ans.status_code == 200, ans.text
    assert ans.json()["status"] == "answered" and ans.json()["resolution"] == "answered"
    # A second answer attempt is refused (the batch is closed).
    assert (
        api["client"]
        .post(
            f"/api/v1/projects/{pid}/clarifications/answers",
            json={"answers": [{"question_id": qid, "answer": "again"}]},
            headers=api["token_for"]("owner"),
        )
        .status_code
        == 409
    )


def test_answer_rejects_foreign_question_id(api: dict[str, Any]) -> None:
    pid = _project(api)
    _open(
        api, pid, {"raw_text": "req", "proposed_questions": [{"topic": "scope", "question": "?"}]}
    )
    r = api["client"].post(
        f"/api/v1/projects/{pid}/clarifications/answers",
        json={"answers": [{"question_id": str(uuid.uuid4()), "answer": "x"}]},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 400


def test_timeout_proceeds_with_flagged_assumptions(api: dict[str, Any]) -> None:
    """TITAN-WFL-008: an unanswered batch past its deadline is resolved with
    best-judgment assumptions flagged high-visibility for the release approval."""
    pid = _project(api)
    _open(
        api,
        pid,
        {"raw_text": "req", "proposed_questions": [{"topic": "scope", "question": "How big?"}]},
    )

    async def _age_and_resolve() -> int:
        # Push the deadline into the past (mutable workflow state), then resolve.
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "UPDATE clarification_batches SET deadline_at = now() - interval '1 hour' "
                    "WHERE project_id = :p"
                ),
                {"p": uuid.UUID(pid)},
            )
        return await clarification.resolve_timed_out_batches(api["engine"])

    resolved = run_async(_age_and_resolve)
    assert resolved == 1
    got = (
        api["client"]
        .get(f"/api/v1/projects/{pid}/clarifications", headers=api["token_for"]("owner"))
        .json()
    )
    assert got["status"] == "timed_out" and got["resolution"] == "timeout_best_judgment"
    assert any(
        a["source"] == "timeout_best_judgment" and a["high_visibility"] for a in got["assumptions"]
    )


def test_timeout_is_idempotent_and_skips_answered(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _open(
        api, pid, {"raw_text": "req", "proposed_questions": [{"topic": "scope", "question": "?"}]}
    )
    qid = r.json()["questions"][0]["id"]
    api["client"].post(
        f"/api/v1/projects/{pid}/clarifications/answers",
        json={"answers": [{"question_id": qid, "answer": "big"}]},
        headers=api["token_for"]("owner"),
    )

    async def _age_and_resolve() -> int:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "UPDATE clarification_batches SET deadline_at = now() - interval '1 hour' "
                    "WHERE project_id = :p"
                ),
                {"p": uuid.UUID(pid)},
            )
        return await clarification.resolve_timed_out_batches(api["engine"])

    # Answered batch is not 'open', so the timeout resolver leaves it alone.
    assert run_async(_age_and_resolve) == 0


def test_submit_requires_advance_permission(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _open(api, pid, {"raw_text": "req"}, token="viewer").status_code == 403
