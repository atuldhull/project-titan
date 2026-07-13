"""The TITAN-WFL-006 traceability table generator.

An automatically generated view over the handoff artifacts: every brief feature
through its stories → tasks → PRs → tests → docs, with gap detection so any
MUST-feature with a broken chain is flagged. Derived, never hand-maintained
(§5, TITAN-WFL-020): the structural links (feature→story→task→PR) come from the
ProjectBrief / ProductBacklog / SprintPlan / MergeCandidateManifest artifacts; the
tests + docs come from the CEO's ReleaseApproval traceability where present.

Pure over the artifact bodies the service loads — no DB access here."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from titan.handoffs.schemas import TraceRow


class Traceability(BaseModel):
    """The generated traceability table + gap list (TITAN-WFL-006)."""

    rows: list[TraceRow]
    #: MUST-features whose chain is incomplete (no story / task / PR / test).
    gaps: list[str]


def _rows(body: Mapping[str, Any] | None, key: str) -> list[Mapping[str, Any]]:
    if body is None:
        return []
    return [r for r in body.get(key, []) if isinstance(r, Mapping)]


def build_traceability(
    *,
    brief: Mapping[str, Any] | None,
    backlog: Mapping[str, Any] | None,
    plan: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
    release_approval: Mapping[str, Any] | None,
) -> Traceability:
    """Assemble the traceability table from the (already-normalized) artifact bodies.

    Each row chains one brief feature → the stories that cover it → the tasks that
    implement those stories → the PRs that merged those tasks → the acceptance tests
    and docs the release approval declares for the feature. A MUST-feature is a gap
    when any earlier link (story/task/PR) is missing or it declares no test."""
    features = _rows(brief, "features")
    stories = _rows(backlog, "stories")
    tasks = _rows(plan, "tasks")
    prs = _rows(manifest, "prs")
    declared = {str(row.get("feature_id")): row for row in _rows(release_approval, "traceability")}

    # story.id -> its tasks; task.id -> its PR numbers.
    tasks_by_story: dict[str, list[str]] = {}
    for task in tasks:
        tasks_by_story.setdefault(str(task.get("story_id")), []).append(str(task.get("id")))
    prs_by_task: dict[str, list[int]] = {}
    for pr in prs:
        try:
            number = int(pr.get("number"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        prs_by_task.setdefault(str(pr.get("task_id")), []).append(number)

    rows: list[TraceRow] = []
    gaps: list[str] = []
    for feature in features:
        feature_id = str(feature.get("id"))
        is_must = feature.get("moscow") == "must"
        story_ids = sorted(
            str(s.get("id"))
            for s in stories
            if feature_id in {str(f) for f in s.get("covers_features", [])}
        )
        task_ids = sorted({t for sid in story_ids for t in tasks_by_story.get(sid, [])})
        pr_numbers = sorted({n for tid in task_ids for n in prs_by_task.get(tid, [])})
        declared_row = declared.get(feature_id, {})
        test_ids = [str(t) for t in declared_row.get("test_ids", [])]
        doc_refs = [str(d) for d in declared_row.get("doc_refs", [])]
        rows.append(
            TraceRow(
                feature_id=feature_id,
                story_ids=story_ids,
                task_ids=task_ids,
                pr_numbers=pr_numbers,
                test_ids=test_ids,
                doc_refs=doc_refs,
            )
        )
        if is_must:
            gaps.extend(_feature_gaps(feature_id, story_ids, task_ids, pr_numbers, test_ids))

    return Traceability(rows=rows, gaps=sorted(gaps))


def _feature_gaps(
    feature_id: str,
    story_ids: Sequence[str],
    task_ids: Sequence[str],
    pr_numbers: Sequence[int],
    test_ids: Sequence[str],
) -> list[str]:
    if not story_ids:
        return [f"MUST-feature '{feature_id}' is covered by no story."]
    if not task_ids:
        return [f"MUST-feature '{feature_id}' has stories but no implementing task."]
    if not pr_numbers:
        return [f"MUST-feature '{feature_id}' has tasks but no merged PR."]
    if not test_ids:
        return [f"MUST-feature '{feature_id}' traces to no acceptance test."]
    return []


__all__ = ["Traceability", "build_traceability"]
