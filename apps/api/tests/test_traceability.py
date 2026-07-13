"""The TITAN-WFL-006 traceability generator (Phase 49). Pure — no DB, no Docker."""

from typing import Any

from titan.handoffs.traceability import build_traceability


def _brief(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"features": features}


def test_full_chain_has_no_gaps_for_a_traced_must_feature() -> None:
    trace = build_traceability(
        brief=_brief([{"id": "F1", "moscow": "must"}]),
        backlog={"stories": [{"id": "S1", "covers_features": ["F1"]}]},
        plan={"tasks": [{"id": "T1", "story_id": "S1"}]},
        manifest={"prs": [{"number": 7, "task_id": "T1"}]},
        release_approval={
            "traceability": [
                {"feature_id": "F1", "test_ids": ["test_login"], "doc_refs": ["README"]}
            ]
        },
    )
    assert trace.gaps == []
    row = next(r for r in trace.rows if r.feature_id == "F1")
    assert row.story_ids == ["S1"]
    assert row.task_ids == ["T1"]
    assert row.pr_numbers == [7]
    assert row.test_ids == ["test_login"]
    assert row.doc_refs == ["README"]


def test_gap_detection_reports_each_broken_link() -> None:
    trace = build_traceability(
        brief=_brief(
            [
                {"id": "NOSTORY", "moscow": "must"},
                {"id": "NOTASK", "moscow": "must"},
                {"id": "NOPR", "moscow": "must"},
                {"id": "NOTEST", "moscow": "must"},
            ]
        ),
        backlog={
            "stories": [
                {"id": "S2", "covers_features": ["NOTASK"]},
                {"id": "S3", "covers_features": ["NOPR"]},
                {"id": "S4", "covers_features": ["NOTEST"]},
            ]
        },
        plan={"tasks": [{"id": "T3", "story_id": "S3"}, {"id": "T4", "story_id": "S4"}]},
        manifest={"prs": [{"number": 1, "task_id": "T4"}]},
        release_approval={"traceability": []},
    )
    joined = " | ".join(trace.gaps)
    assert "'NOSTORY' is covered by no story" in joined
    assert "'NOTASK' has stories but no implementing task" in joined
    assert "'NOPR' has tasks but no merged PR" in joined
    assert "'NOTEST' traces to no acceptance test" in joined


def test_should_features_never_produce_gaps() -> None:
    # Gap detection is a MUST-feature guarantee; a SHOULD-feature without a chain is
    # not a release blocker.
    trace = build_traceability(
        brief=_brief([{"id": "S", "moscow": "should"}]),
        backlog=None,
        plan=None,
        manifest=None,
        release_approval=None,
    )
    assert trace.gaps == []
    assert len(trace.rows) == 1 and trace.rows[0].feature_id == "S"


def test_empty_project_yields_empty_table() -> None:
    trace = build_traceability(
        brief=None, backlog=None, plan=None, manifest=None, release_approval=None
    )
    assert trace.rows == [] and trace.gaps == []
