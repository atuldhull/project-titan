"""Phase 42 — the twelve handoff artifact schemas (Vol 6 §2.3, TITAN-WFL-003).

Pure, hermetic (no DB): every schema round-trips a representative instance built
from the §2.3 field lists (construct → serialize → re-parse identical), rejects
unknown fields (extra="forbid"), enforces its key constraints, and produces a
field-level error report on a missing required field.
"""

import pytest
from pydantic import ValidationError

from titan.handoffs import gates
from titan.handoffs import schemas as s

# One representative instance per artifact type, from the §2.3 field lists.
EXAMPLES: dict[str, dict] = {
    "UserRequest": dict(raw_text="Build me a Netflix clone", org="acme"),
    "ProjectBrief": dict(
        vision="A local-first streaming app.",
        target_users=["viewers"],
        features=[dict(id="F1", name="Play video", moscow="must", success_criterion="starts <2s")],
        non_goals=["no live TV"],
        constraints=dict(local_first=True, compliance=["gdpr"]),
        success_criteria=["p95 start < 2s"],
        assumptions=[dict(statement="payments are mocked", user_overridable=True)],
    ),
    "ProductBacklog": dict(
        stories=[
            dict(
                id="S1",
                as_a="viewer",
                i_want="to play a video",
                so_that="I can watch",
                acceptance_criteria=[dict(given="a video", when="I press play", then="it plays")],
                priority=1,
                points=3,
                covers_features=["F1"],
            )
        ]
    ),
    "SprintPlan": dict(
        sprints=[dict(number=1, goal="walking skeleton", task_ids=["T1"], walking_skeleton=True)],
        tasks=[
            dict(
                id="T1",
                title="play endpoint",
                estimate_days=0.5,
                assignee_role="backend",
                story_id="S1",
                sprint_number=1,
            )
        ],
        dependencies=[],
        milestones=[dict(id="M1", title="MVP")],
    ),
    "ArchitecturePackage": dict(
        component_diagram_ascii="[api]->[db]",
        components=[dict(name="api", layer="frameworks", depends_on=["db"])],
        api_contracts=[dict(path="/play", method="POST", operation={})],
        database=dict(
            tables=[dict(name="videos", columns=["id"], indexes=[])], migration_order=["0001"]
        ),
        frontend=dict(routes=[dict(path="/", component="Home")], state_management="redux"),
        adrs=[dict(id="A1", title="use pg", decision="pg", rationale="fits")],
    ),
    "MergeCandidateManifest": dict(
        prs=[dict(number=1, title="play", task_id="T1", branch="feat/play", sha="abc1234")],
        changelog="- play",
        lockfiles=["uv.lock"],
        migrations=["0001"],
    ),
    "QAReport": dict(
        test_matrix=[dict(suite="e2e", passed=10, failed=0, total=10)],
        defects=[],
        flake_log=[],
        performance_baseline=[dict(name="start", value=1.2, unit="s")],
    ),
    "SecurityClearance": dict(
        sast=[],
        dependency_audit=[],
        secret_scan=dict(clean=True),
        owasp=[dict(category="A01", status="pass", evidence="n/a")],
        rbac_verified=True,
    ),
    "ReleaseReview": dict(
        verdict="APPROVED",
        architecture_boundary_ok=True,
        duplication_findings=[],
        public_api_consistent=True,
        changelog_accurate=True,
    ),
    "DeploymentRecord": dict(
        image_digests=[dict(image="api", digest="sha256:x")],
        environment="production",
        applied_migrations=["0001"],
        smoke_tests=dict(passed=5, failed=0),
        health_checks="green",
        rollback_pointer="v1.0.0",
    ),
    "DocBundle": dict(
        docs=[dict(kind="readme", path="README.md")],
        documented_endpoints=["/play"],
        adr_index=["A1"],
        quickstart_verified=True,
    ),
    "ReleaseApproval": dict(
        approved=True,
        summary="ships",
        success_criteria_met=[dict(criterion="p95<2s", met=True)],
        traceability=[
            dict(
                feature_id="F1",
                story_ids=["S1"],
                task_ids=["T1"],
                pr_numbers=[1],
                test_ids=["e2e-1"],
                doc_refs=["README.md"],
            )
        ],
    ),
}


def test_all_twelve_artifact_types_have_an_example() -> None:
    assert set(EXAMPLES) == set(s.ALL_ARTIFACT_SCHEMAS)
    assert len(s.ALL_ARTIFACT_SCHEMAS) == 12
    assert len(s.ARTIFACT_SCHEMAS) == 11  # producing stages (handover emits none)


@pytest.mark.parametrize("type_name", sorted(EXAMPLES))
def test_schema_round_trips(type_name: str) -> None:
    """construct → serialize → re-parse identical, with a versioned, typed payload."""
    model = s.ALL_ARTIFACT_SCHEMAS[type_name]
    dumped = model(**EXAMPLES[type_name]).model_dump(mode="json")
    reparsed = model(**dumped).model_dump(mode="json")
    assert reparsed == dumped
    assert dumped["artifact_type"] == type_name
    assert dumped["schema_version"] == 1


@pytest.mark.parametrize("type_name", sorted(EXAMPLES))
def test_schema_forbids_unknown_fields(type_name: str) -> None:
    model = s.ALL_ARTIFACT_SCHEMAS[type_name]
    with pytest.raises(ValidationError):
        model(**{**EXAMPLES[type_name], "surprise_field": "x"})


def test_project_brief_vision_word_cap() -> None:
    long_vision = " ".join(["word"] * 301)
    with pytest.raises(ValidationError):
        s.ProjectBrief(
            vision=long_vision,
            target_users=["u"],
            features=[dict(id="F1", name="n", moscow="must")],
            non_goals=["ng"],
            constraints=dict(),
            success_criteria=["sc"],
        )


def test_story_points_must_be_fibonacci() -> None:
    with pytest.raises(ValidationError):
        s.UserStory(
            id="S1",
            as_a="u",
            i_want="w",
            so_that="s",
            acceptance_criteria=[dict(given="g", when="w", then="t")],
            priority=1,
            points=4,  # not on the fib scale
        )


def test_planned_task_estimate_capped_at_one_day() -> None:
    with pytest.raises(ValidationError):
        s.PlannedTask(
            id="T1",
            title="t",
            estimate_days=1.5,
            assignee_role="backend",
            story_id="S1",
            sprint_number=1,
        )


# --- the schema gate: field-level error report (TITAN-WFL-003) --------------


def test_validate_schema_passes_and_normalizes() -> None:
    result, normalized = gates.validate_schema("ceo_vision", EXAMPLES["ProjectBrief"])
    assert result.passed and normalized is not None
    assert normalized["artifact_type"] == "ProjectBrief" and normalized["schema_version"] == 1


def test_validate_schema_reports_missing_fields() -> None:
    """A missing required field yields a field-level error report, not a crash."""
    result, normalized = gates.validate_schema("ceo_vision", {"target_users": ["u"]})
    assert not result.passed and normalized is None
    joined = " ".join(result.reasons)
    assert "vision" in joined and "features" in joined  # named missing fields
    # Deterministically ordered for a stable report.
    assert result.reasons == sorted(result.reasons)


def test_validate_schema_unknown_stage_fails_closed() -> None:
    result, normalized = gates.validate_schema("handover", {})
    assert not result.passed and normalized is None
