"""The twelve versioned handoff artifact schemas (Vol 6 §2.3, TITAN-WFL-003).

Every handoff Hn carries a Pydantic-v2 artifact that MUST validate before its
acceptance gate runs; a missing/ill-typed field is a schema-validation failure
that the executor turns into a gate failure with a field-level error report.

Each schema is fully fielded from the §2.3 field lists and versioned via a
``schema_version`` field so the stored JSON round-trips (construct → serialize →
re-parse identical) and future schema revisions are distinguishable. Every model
is ``extra="forbid"`` so an unknown field is rejected rather than silently
dropped. ``ARTIFACT_SCHEMAS`` maps each producing pipeline stage to the artifact
it emits; ``ALL_ARTIFACT_SCHEMAS`` is the by-type registry the round-trip suite
and the store use.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Fibonacci story-point scale (Vol 6 §2.3 H3 / TITAN-BCK-013).
StoryPoints = Literal[1, 2, 3, 5, 8, 13]
Moscow = Literal["must", "should", "could", "wont"]
Severity = Literal["low", "medium", "high", "critical"]


class _Artifact(BaseModel):
    """Base for every handoff artifact: forbids unknown fields and carries the
    version stamp so the persisted document is self-describing and round-trips."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1


# --- H1: UserRequest --------------------------------------------------------


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=200)
    storage_ref: str = Field(min_length=1)


class UserRequest(_Artifact):
    artifact_type: Literal["UserRequest"] = "UserRequest"
    raw_text: str = Field(min_length=1)
    attachments: list[Attachment] = Field(default_factory=list, max_length=50)
    org: str = Field(min_length=1, max_length=200)
    org_context: dict[str, str] = Field(default_factory=dict)


# --- H2: ProjectBrief -------------------------------------------------------


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    moscow: Moscow
    # A measurable success criterion — required for MUST features (checked at G2).
    success_criterion: str | None = Field(default=None, max_length=2000)


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = Field(default=None, ge=0)
    local_first: bool = True
    compliance: list[str] = Field(default_factory=list)
    # Whether the canonical stack applies (False only when the user overrode it).
    canonical_stack: bool = True


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    user_overridable: bool = True


class ProjectBrief(_Artifact):
    artifact_type: Literal["ProjectBrief"] = "ProjectBrief"
    # Product vision, ≤300 words (§2.3 H2).
    vision: str = Field(min_length=1)
    target_users: list[str] = Field(min_length=1, max_length=50)
    features: list[Feature] = Field(min_length=1, max_length=200)
    non_goals: list[str] = Field(min_length=1, max_length=100)
    constraints: Constraints
    success_criteria: list[str] = Field(min_length=1, max_length=100)
    # §2.4 TITAN-WFL-007: unknowns resolved as logged, user-overridable assumptions.
    assumptions: list[Assumption] = Field(default_factory=list, max_length=100)

    @field_validator("vision")
    @classmethod
    def _vision_word_cap(cls, value: str) -> str:
        if len(value.split()) > 300:
            raise ValueError("vision must be ≤300 words")
        return value


# --- H3: ProductBacklog -----------------------------------------------------


class GherkinAC(BaseModel):
    model_config = ConfigDict(extra="forbid")

    given: str = Field(min_length=1, max_length=2000)
    when: str = Field(min_length=1, max_length=2000)
    then: str = Field(min_length=1, max_length=2000)


class UserStory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    as_a: str = Field(min_length=1, max_length=500)
    i_want: str = Field(min_length=1, max_length=1000)
    so_that: str = Field(min_length=1, max_length=1000)
    acceptance_criteria: list[GherkinAC] = Field(min_length=1, max_length=50)
    priority: int = Field(ge=1, le=5)
    points: StoryPoints
    # Traceability to the brief's features (G3 coverage check).
    covers_features: list[str] = Field(default_factory=list, max_length=50)


class ProductBacklog(_Artifact):
    artifact_type: Literal["ProductBacklog"] = "ProductBacklog"
    stories: list[UserStory] = Field(min_length=1, max_length=500)


# --- H4: SprintPlan + TaskGraph ---------------------------------------------


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    # Each task ≤1 ideal agent-day (§2.3 H4).
    estimate_days: float = Field(gt=0, le=1.0)
    assignee_role: str = Field(min_length=1, max_length=100)
    story_id: str = Field(min_length=1, max_length=100)
    sprint_number: int = Field(ge=1)
    milestone_id: str | None = Field(default=None, max_length=100)


class TaskEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=100)
    depends_on_id: str = Field(min_length=1, max_length=100)


class SprintSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=2000)
    task_ids: list[str] = Field(default_factory=list, max_length=200)
    # Sprint 1 carries a thin end-to-end walking-skeleton slice (G4).
    walking_skeleton: bool = False


class MilestoneRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)


class SprintPlan(_Artifact):
    """H4 combines the SprintPlan and TaskGraph into one handoff artifact."""

    artifact_type: Literal["SprintPlan"] = "SprintPlan"
    sprints: list[SprintSlice] = Field(min_length=1, max_length=100)
    tasks: list[PlannedTask] = Field(min_length=1, max_length=2000)
    dependencies: list[TaskEdge] = Field(default_factory=list, max_length=5000)
    milestones: list[MilestoneRef] = Field(default_factory=list, max_length=100)


# --- H5: ArchitecturePackage ------------------------------------------------

CleanLayer = Literal["entities", "use_cases", "interface_adapters", "frameworks"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class Component(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    layer: CleanLayer
    depends_on: list[str] = Field(default_factory=list, max_length=100)


class ApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    method: HttpMethod
    # An OpenAPI 3.1 operation object (validated as OpenAPI at G5, not here).
    operation: dict[str, object] = Field(default_factory=dict)


class TableDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    columns: list[str] = Field(min_length=1, max_length=200)
    indexes: list[str] = Field(default_factory=list, max_length=100)


class DbSchemaDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableDraft] = Field(default_factory=list, max_length=500)
    migration_order: list[str] = Field(default_factory=list, max_length=500)


class RouteDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    component: str = Field(min_length=1, max_length=200)


class FrontendPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[RouteDef] = Field(default_factory=list, max_length=500)
    state_management: str = Field(min_length=1, max_length=500)


class Adr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    decision: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)


class ArchitecturePackage(_Artifact):
    artifact_type: Literal["ArchitecturePackage"] = "ArchitecturePackage"
    component_diagram_ascii: str = Field(min_length=1)
    components: list[Component] = Field(min_length=1, max_length=500)
    api_contracts: list[ApiContract] = Field(default_factory=list, max_length=1000)
    database: DbSchemaDraft
    frontend: FrontendPlan
    adrs: list[Adr] = Field(default_factory=list, max_length=200)


# --- H6: MergeCandidateManifest ---------------------------------------------


class MergedPr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    task_id: str = Field(min_length=1, max_length=100)
    branch: str = Field(min_length=1, max_length=300)
    sha: str = Field(min_length=7, max_length=64)


class MergeCandidateManifest(_Artifact):
    artifact_type: Literal["MergeCandidateManifest"] = "MergeCandidateManifest"
    prs: list[MergedPr] = Field(min_length=1, max_length=1000)
    changelog: str = Field(min_length=1)
    lockfiles: list[str] = Field(default_factory=list, max_length=100)
    migrations: list[str] = Field(default_factory=list, max_length=500)


# --- H7: QAReport -----------------------------------------------------------

TestSuite = Literal["integration", "e2e", "regression", "performance"]
DefectSeverity = Literal["P0", "P1", "P2", "P3"]


class TestSuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: TestSuite
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    total: int = Field(ge=0)


class Defect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    severity: DefectSeverity
    title: str = Field(min_length=1, max_length=1000)
    owner_role: str = Field(min_length=1, max_length=100)


class PerfMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    value: float
    unit: str = Field(min_length=1, max_length=50)


class QAReport(_Artifact):
    artifact_type: Literal["QAReport"] = "QAReport"
    test_matrix: list[TestSuiteResult] = Field(min_length=1, max_length=50)
    defects: list[Defect] = Field(default_factory=list, max_length=1000)
    flake_log: list[str] = Field(default_factory=list, max_length=500)
    performance_baseline: list[PerfMetric] = Field(default_factory=list, max_length=200)


# --- H8: SecurityClearance --------------------------------------------------

OwaspStatus = Literal["pass", "fail", "na"]


class SastFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    severity: Severity
    title: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)


class CveFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str = Field(min_length=1, max_length=300)
    cve_id: str = Field(min_length=1, max_length=50)
    severity: Severity


class OwaspItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=200)
    status: OwaspStatus
    evidence: str = Field(min_length=1, max_length=4000)


class SecretScan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clean: bool
    findings: list[str] = Field(default_factory=list, max_length=500)


class SecurityClearance(_Artifact):
    artifact_type: Literal["SecurityClearance"] = "SecurityClearance"
    sast: list[SastFinding] = Field(default_factory=list, max_length=2000)
    dependency_audit: list[CveFinding] = Field(default_factory=list, max_length=2000)
    secret_scan: SecretScan
    owasp: list[OwaspItem] = Field(min_length=1, max_length=20)
    rbac_verified: bool
    rbac_notes: str = Field(default="", max_length=4000)


# --- H9: ReleaseReview ------------------------------------------------------

ReviewVerdict = Literal["APPROVED", "REJECTED", "APPROVED_WITH_COMMENTS"]


class ReleaseReview(_Artifact):
    artifact_type: Literal["ReleaseReview"] = "ReleaseReview"
    verdict: ReviewVerdict
    architecture_boundary_ok: bool
    duplication_findings: list[str] = Field(default_factory=list, max_length=500)
    public_api_consistent: bool
    changelog_accurate: bool
    # APPROVED_WITH_COMMENTS logs these as technical-debt entries (Section 5).
    comments: list[str] = Field(default_factory=list, max_length=500)
    rework_tickets: list[str] = Field(default_factory=list, max_length=500)


# --- H10: DeploymentRecord --------------------------------------------------

DeployEnv = Literal["staging", "production"]
HealthStatus = Literal["green", "red"]


class ImageDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = Field(min_length=1, max_length=500)
    digest: str = Field(min_length=1, max_length=200)


class SmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)


class DeploymentRecord(_Artifact):
    artifact_type: Literal["DeploymentRecord"] = "DeploymentRecord"
    image_digests: list[ImageDigest] = Field(min_length=1, max_length=200)
    environment: DeployEnv
    applied_migrations: list[str] = Field(default_factory=list, max_length=500)
    smoke_tests: SmokeResult
    health_checks: HealthStatus
    grafana_links: list[str] = Field(default_factory=list, max_length=100)
    rollback_pointer: str = Field(min_length=1, max_length=500)


# --- H11: DocBundle ---------------------------------------------------------

DocKind = Literal[
    "readme",
    "architecture_guide",
    "api_reference",
    "user_manual",
    "runbook",
    "contribution_guide",
    "troubleshooting_guide",
    "release_notes",
    "adr_index",
]


class DocEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DocKind
    path: str = Field(min_length=1, max_length=500)
    present: bool = True


class DocBundle(_Artifact):
    artifact_type: Literal["DocBundle"] = "DocBundle"
    docs: list[DocEntry] = Field(min_length=1, max_length=50)
    # G11 coverage: every public endpoint documented, every ADR indexed.
    documented_endpoints: list[str] = Field(default_factory=list, max_length=2000)
    adr_index: list[str] = Field(default_factory=list, max_length=500)
    quickstart_verified: bool = False


# --- H12: ReleaseApproval ---------------------------------------------------


class CriterionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(min_length=1, max_length=2000)
    met: bool


class TraceRow(BaseModel):
    """One row of the TITAN-WFL-006 traceability table: a brief feature through
    stories → tasks → PRs → tests → docs."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(min_length=1, max_length=100)
    story_ids: list[str] = Field(default_factory=list, max_length=200)
    task_ids: list[str] = Field(default_factory=list, max_length=500)
    pr_numbers: list[int] = Field(default_factory=list, max_length=500)
    test_ids: list[str] = Field(default_factory=list, max_length=1000)
    doc_refs: list[str] = Field(default_factory=list, max_length=200)


class ReleaseApproval(_Artifact):
    artifact_type: Literal["ReleaseApproval"] = "ReleaseApproval"
    approved: bool
    summary: str = Field(min_length=1, max_length=8000)
    success_criteria_met: list[CriterionCheck] = Field(min_length=1, max_length=100)
    traceability: list[TraceRow] = Field(min_length=1, max_length=500)
    # §2.4 TITAN-WFL-008: assumption-driven decisions restated at release.
    assumptions_restated: list[str] = Field(default_factory=list, max_length=100)


# --- registries -------------------------------------------------------------

ArtifactModel = Annotated[
    UserRequest
    | ProjectBrief
    | ProductBacklog
    | SprintPlan
    | ArchitecturePackage
    | MergeCandidateManifest
    | QAReport
    | SecurityClearance
    | ReleaseReview
    | DeploymentRecord
    | DocBundle
    | ReleaseApproval,
    Field(discriminator="artifact_type"),
]

# The artifact each PRODUCING pipeline stage emits (Vol 6 §2.1 stage → §2.3 Hn).
# UserRequest (H1) is the intake artifact submitted to START the pipeline;
# ``handover`` is terminal and emits no artifact.
ARTIFACT_SCHEMAS: dict[str, type[_Artifact]] = {
    "ceo_vision": ProjectBrief,
    "pm_backlog": ProductBacklog,
    "pmgr_plan": SprintPlan,
    "architect_design": ArchitecturePackage,
    "engineering": MergeCandidateManifest,
    "qa": QAReport,
    "security": SecurityClearance,
    "code_review": ReleaseReview,
    "devops_deploy": DeploymentRecord,
    "documentation": DocBundle,
    "ceo_release": ReleaseApproval,
}

# Every artifact by its type name — the round-trip suite + the store use this.
ALL_ARTIFACT_SCHEMAS: dict[str, type[_Artifact]] = {
    schema.model_fields["artifact_type"].default: schema
    for schema in (
        UserRequest,
        ProjectBrief,
        ProductBacklog,
        SprintPlan,
        ArchitecturePackage,
        MergeCandidateManifest,
        QAReport,
        SecurityClearance,
        ReleaseReview,
        DeploymentRecord,
        DocBundle,
        ReleaseApproval,
    )
}


def schema_for_stage(stage: str) -> type[_Artifact] | None:
    """The artifact schema a producing stage emits, or None (handover/unknown)."""
    return ARTIFACT_SCHEMAS.get(stage)


__all__ = [
    "ALL_ARTIFACT_SCHEMAS",
    "ARTIFACT_SCHEMAS",
    "Adr",
    "ApiContract",
    "ArchitecturePackage",
    "ArtifactModel",
    "Assumption",
    "Attachment",
    "Component",
    "Constraints",
    "CriterionCheck",
    "CveFinding",
    "DbSchemaDraft",
    "Defect",
    "DeploymentRecord",
    "DocBundle",
    "DocEntry",
    "Feature",
    "FrontendPlan",
    "GherkinAC",
    "ImageDigest",
    "MergeCandidateManifest",
    "MergedPr",
    "MilestoneRef",
    "OwaspItem",
    "PerfMetric",
    "PlannedTask",
    "ProductBacklog",
    "ProjectBrief",
    "QAReport",
    "ReleaseApproval",
    "ReleaseReview",
    "RouteDef",
    "SastFinding",
    "SecretScan",
    "SecurityClearance",
    "SmokeResult",
    "SprintPlan",
    "SprintSlice",
    "TableDraft",
    "TaskEdge",
    "TestSuiteResult",
    "TraceRow",
    "UserRequest",
    "UserStory",
    "schema_for_stage",
]
