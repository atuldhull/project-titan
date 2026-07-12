"""The concrete acceptance gates G1–G5 (Vol 6 §2.3 H1–H5).

Each gate is a **pure** predicate over a :class:`~titan.handoffs.gates.GateContext`
— it reads the artifact, any prior-stage artifacts, the active constitution, and
tunables, and returns a :class:`~titan.handoffs.gates.GateResult` whose ``reasons``
carry the §2.3 failure-path payload the producer must act on:

- **G1** (UserRequest, H1): non-empty and not a duplicate of an active project.
  Failure routes at the API layer (the clarification intake rejects it); ambiguity
  is *not* failure — it drives the clarification policy (§2.4).
- **G2** (ProjectBrief, H2): every MUST-feature has a measurable success criterion
  and non-goals is non-empty (the §2.3 canonical-stack clause is a waiver the
  schema's ``canonical_stack`` flag already satisfies). Failure returns the brief
  to the CEO with **gap annotations**.
- **G3** (ProductBacklog, H3): every story INVEST-checks (actor, outcome, ≤13
  points, ≥1 testable Gherkin AC, valid feature refs) and every MUST-feature is
  covered by ≥1 story. Failure returns the **specific INVEST violations** to the PM
  (partial acceptance: passing stories are listed as accepted).
- **G4** (SprintPlan/TaskGraph, H4): the task DAG is acyclic, every story maps to
  ≥1 task, sprint 1 carries a walking-skeleton slice, and no sprint exceeds
  capacity. Failure returns the **offending subgraph** to the Project Manager.
- **G5** (ArchitecturePackage, H5): complies with the constitution (checklist over
  §6), every TaskGraph task references a package component, and every API contract
  validates as an OpenAPI 3.1 operation. Failure returns **numbered objections** to
  the Architect; the ledger records the constitution version judged against.

Gates run *after* the schema gate, so a gate never sees an ill-typed artifact.
"""

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from titan.handoffs.gates import Gate, GateContext, GateResult, register_gate

# --- helpers ----------------------------------------------------------------


def _norm(text: str) -> str:
    """Whitespace/case-normalized text for deterministic equality comparison."""
    return " ".join(text.lower().split())


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _features(brief: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    feats = brief.get("features", [])
    return [f for f in feats if isinstance(f, Mapping)]


def _must_feature_ids(brief: Mapping[str, Any]) -> list[str]:
    return [str(f["id"]) for f in _features(brief) if f.get("moscow") == "must"]


# --- G1: UserRequest --------------------------------------------------------


@dataclass(frozen=True)
class UserRequestGate:
    """G1 (H1): the request is non-empty and not a duplicate of an active project.
    A duplicate is detected as an exact normalized-text match against another
    non-archived project's request/brief (semantic dedup is additive, later)."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        raw = ctx.body.get("raw_text", "")
        if _blank(raw):
            reasons.append("G1: the request is empty.")
        else:
            normalized = _norm(str(raw))
            if normalized in {_norm(t) for t in ctx.active_project_texts}:
                reasons.append(
                    "G1: the request duplicates an active project (identical brief text). "
                    "Resume or archive the existing project instead of opening a duplicate."
                )
        return GateResult(
            passed=not reasons,
            reasons=reasons,
            evaluator_agent="ceo",
            evaluator_model="rules",
            prompt_version="G1/v1",
        )


# --- G2: ProjectBrief -------------------------------------------------------


@dataclass(frozen=True)
class ProjectBriefGate:
    """G2 (H2, §2.3): every MUST-feature carries a measurable success criterion and
    the non-goals section is non-empty. The third §2.3 clause — "constraints include
    the canonical stack unless the user overrode it" — is a waiver structurally
    satisfied by ``Constraints.canonical_stack`` (True = stack applies, False = the
    user's recorded override), so it needs no gate check. Reasons are gap
    annotations the CEO revises against; they name features by their stable id
    only, never the free-text display name (TITAN-OPS-016)."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        brief = ctx.body
        gaps: list[str] = []
        for feat in _features(brief):
            if feat.get("moscow") == "must" and _blank(feat.get("success_criterion")):
                gaps.append(
                    f"G2: MUST-feature '{feat.get('id')}' has no measurable success criterion."
                )
        if not brief.get("non_goals"):
            gaps.append(
                "G2: the non-goals section is empty — state what is explicitly out of scope."
            )
        return GateResult(
            passed=not gaps,
            reasons=gaps,
            evaluator_agent="pm",
            evaluator_model="rules",
            prompt_version="G2/v1",
        )


# --- G3: ProductBacklog -----------------------------------------------------


def _story_invest_violations(story: Mapping[str, Any], brief_feature_ids: set[str]) -> list[str]:
    sid = story.get("id")
    out: list[str] = []
    if _blank(story.get("as_a")):
        out.append(f"G3: story '{sid}' has no actor (INVEST: 'As a …').")
    if _blank(story.get("i_want")) or _blank(story.get("so_that")):
        out.append(f"G3: story '{sid}' has no outcome (INVEST: 'I want …, so that …').")
    points = story.get("points")
    if not isinstance(points, int) or points > 13:
        out.append(f"G3: story '{sid}' is not small enough (points={points}, INVEST ≤13).")
    acs = story.get("acceptance_criteria", [])
    if not acs:
        out.append(f"G3: story '{sid}' has no acceptance criteria (INVEST: ≥1 testable AC).")
    for i, ac in enumerate(acs, start=1):
        if not isinstance(ac, Mapping):
            continue
        clauses = (str(ac.get("given", "")), str(ac.get("when", "")), str(ac.get("then", "")))
        if len({_norm(c) for c in clauses}) == 1:
            out.append(
                f"G3: story '{sid}' AC #{i} is not testable (given/when/then are identical)."
            )
    for fid in story.get("covers_features", []):
        if str(fid) not in brief_feature_ids:
            out.append(f"G3: story '{sid}' references unknown feature '{fid}'.")
    return out


@dataclass(frozen=True)
class ProductBacklogGate:
    """G3 (H3): INVEST per story + every MUST-feature covered by ≥1 story. Partial
    acceptance — passing stories are reported as accepted; only failing stories and
    uncovered features are returned to the PM."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        brief = ctx.prior_artifacts.get("ceo_vision")
        if brief is None:
            return GateResult(
                passed=False,
                reasons=["G3: the project brief is unavailable — cannot check story coverage."],
                evaluator_agent="project_manager",
                evaluator_model="rules",
                prompt_version="G3/v1",
            )
        feature_ids = {str(f["id"]) for f in _features(brief)}
        stories = [s for s in ctx.body.get("stories", []) if isinstance(s, Mapping)]

        reasons: list[str] = []
        accepted: list[str] = []
        for story in stories:
            violations = _story_invest_violations(story, feature_ids)
            if violations:
                reasons.extend(violations)
            else:
                accepted.append(str(story.get("id")))

        covered: set[str] = set()
        for story in stories:
            covered.update(str(f) for f in story.get("covers_features", []))
        for must_id in _must_feature_ids(brief):
            if must_id not in covered:
                reasons.append(f"G3: MUST-feature '{must_id}' is not covered by any story.")

        if reasons and accepted:
            reasons.append(f"G3: accepted (INVEST-passing) stories: {', '.join(sorted(accepted))}.")
        return GateResult(
            passed=not reasons,
            reasons=reasons,
            evaluator_agent="project_manager",
            evaluator_model="rules",
            prompt_version="G3/v1",
        )


# --- G4: SprintPlan / TaskGraph ---------------------------------------------


def _cyclic_task_ids(task_ids: set[str], edges: Sequence[tuple[str, str]]) -> list[str]:
    """The set of tasks participating in (or blocked behind) a dependency cycle —
    the offending subgraph. Kahn's algorithm: nodes with residual in-degree after a
    topological sweep are exactly those a cycle prevents from ordering. Empty ⇒
    acyclic. Iterative (no recursion-limit risk on large DAGs)."""
    indeg = {t: 0 for t in task_ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for task, dep in edges:
        if task in indeg and dep in indeg:  # dangling edges handled separately
            adj[dep].append(task)
            indeg[task] += 1
    queue = deque(sorted(t for t in task_ids if indeg[t] == 0))
    ordered = 0
    while queue:
        node = queue.popleft()
        ordered += 1
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if ordered == len(task_ids):
        return []
    return sorted(t for t in task_ids if indeg[t] > 0)


@dataclass(frozen=True)
class SprintPlanGate:
    """G4 (H4): acyclic DAG, story→task coverage, sprint-1 walking skeleton, and no
    sprint over capacity. Reasons carry the offending subgraph."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        backlog = ctx.prior_artifacts.get("pm_backlog")
        if backlog is None:
            return GateResult(
                passed=False,
                reasons=["G4: the product backlog is unavailable — cannot check story→task map."],
                evaluator_agent="architect",
                evaluator_model="rules",
                prompt_version="G4/v1",
            )
        plan = ctx.body
        tasks = [t for t in plan.get("tasks", []) if isinstance(t, Mapping)]
        task_ids = {str(t["id"]) for t in tasks}
        reasons: list[str] = []

        # 1) DAG integrity: dangling edges + cycles.
        edges: list[tuple[str, str]] = []
        for edge in plan.get("dependencies", []):
            if not isinstance(edge, Mapping):
                continue
            tid, dep = str(edge.get("task_id")), str(edge.get("depends_on_id"))
            if tid not in task_ids or dep not in task_ids:
                reasons.append(
                    f"G4: dependency edge {tid} -> {dep} references a task not in the plan."
                )
            else:
                edges.append((tid, dep))
        cyclic = _cyclic_task_ids(task_ids, edges)
        if cyclic:
            reasons.append(f"G4: the task dependency graph has a cycle: {', '.join(cyclic)}.")

        # 2) Every story maps to ≥1 task.
        mapped = {str(t.get("story_id")) for t in tasks}
        story_ids = [str(s["id"]) for s in backlog.get("stories", []) if isinstance(s, Mapping)]
        for sid in story_ids:
            if sid not in mapped:
                reasons.append(f"G4: story '{sid}' maps to no task.")

        # 3) Sprint 1 carries a walking-skeleton slice.
        sprints = [s for s in plan.get("sprints", []) if isinstance(s, Mapping)]
        sprint_one = next((s for s in sprints if s.get("number") == 1), None)
        if sprint_one is None:
            reasons.append("G4: there is no sprint numbered 1.")
        elif not sprint_one.get("walking_skeleton"):
            reasons.append(
                "G4: sprint 1 has no walking-skeleton slice (a thin end-to-end path is required)."
            )

        # 4) No sprint over capacity.
        load: dict[int, float] = defaultdict(float)
        for task in tasks:
            num = task.get("sprint_number")
            if isinstance(num, int):
                load[num] += float(task.get("estimate_days", 0.0))
        cap = ctx.sprint_capacity_days
        for num in sorted(load):
            if load[num] > cap + 1e-9:
                over = sorted(str(t["id"]) for t in tasks if t.get("sprint_number") == num)
                reasons.append(
                    f"G4: sprint {num} is over capacity ({load[num]:g} > {cap:g} agent-days); "
                    f"offending tasks: {', '.join(over)}."
                )
        return GateResult(
            passed=not reasons,
            reasons=reasons,
            evaluator_agent="architect",
            evaluator_model="rules",
            prompt_version="G4/v1",
        )


# --- G5: ArchitecturePackage ------------------------------------------------

# Clean Architecture layers, innermost first — a component may depend only on its
# own layer or an inner one (TITAN-WFL-022, dependencies point inward).
_LAYER_ORDER = {"entities": 0, "use_cases": 1, "interface_adapters": 2, "frameworks": 3}

# A focused OpenAPI 3.1 Operation Object schema: an operation MUST declare
# ``responses`` with ≥1 entry, each keyed by a status code / range / ``default``
# and carrying a non-empty ``description`` (the OpenAPI Response Object requirement).
_OPENAPI_OPERATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["responses"],
    "properties": {
        "responses": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": {"pattern": r"^(default|[1-5](?:[0-9]{2}|XX))$"},
            "additionalProperties": {
                "type": "object",
                "required": ["description"],
                "properties": {"description": {"type": "string", "minLength": 1}},
            },
        },
        "parameters": {"type": "array"},
        "tags": {"type": "array"},
    },
}
_OPENAPI_VALIDATOR = Draft202012Validator(_OPENAPI_OPERATION_SCHEMA)


def _operation_error(operation: Mapping[str, Any]) -> str | None:
    """None if ``operation`` is a valid OpenAPI 3.1 operation object, else the
    single best-matching validation message."""
    errors = sorted(_OPENAPI_VALIDATOR.iter_errors(dict(operation)), key=str)
    if not errors:
        return None
    match = best_match(errors)
    return match.message if match is not None else "invalid operation object"


@dataclass(frozen=True)
class ArchitecturePackageGate:
    """G5 (H5): constitution compliance (checklist over §6), task→component coverage,
    and OpenAPI-3.1-valid API contracts. Reasons are numbered objections; the
    result stamps the constitution version judged against onto the ledger."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        pkg = ctx.body
        objections: list[str] = []
        components = [c for c in pkg.get("components", []) if isinstance(c, Mapping)]
        names = {str(c["name"]) for c in components}

        # Checklist item 1 — Clean Architecture inward-only layering (WFL-022).
        for comp in components:
            layer = str(comp.get("layer"))
            for dep in comp.get("depends_on", []):
                dep = str(dep)
                if dep not in names:
                    objections.append(
                        f"Component '{comp.get('name')}' depends on unknown component '{dep}'."
                    )
                    continue
                dep_layer = next(
                    (str(c.get("layer")) for c in components if c.get("name") == dep), None
                )
                if dep_layer is not None and _LAYER_ORDER.get(dep_layer, 0) > _LAYER_ORDER.get(
                    layer, 0
                ):
                    objections.append(
                        f"Component '{comp.get('name')}' ({layer}) depends on '{dep}' "
                        f"({dep_layer}), an outer layer — violates Clean Architecture "
                        "inward-only rule (TITAN-WFL-022)."
                    )

        # Checklist item 2 — CQRS only if justified by an ADR (WFL-026). A genuine
        # CQRS split follows the Command/Query naming convention (…Command / …Query
        # SUFFIX) and has BOTH sides. Matching the suffix (not a bare substring)
        # avoids false positives on common non-CQRS names like a "CommandBus"
        # dispatcher or a SQL "QueryBuilder".
        command_side = [
            c["name"] for c in components if _norm(str(c.get("name"))).endswith("command")
        ]
        query_side = [c["name"] for c in components if _norm(str(c.get("name"))).endswith("query")]
        cqrs_components = command_side + query_side
        if command_side and query_side:
            adr_text = _norm(
                " ".join(
                    f"{a.get('title', '')} {a.get('decision', '')} {a.get('rationale', '')}"
                    for a in pkg.get("adrs", [])
                    if isinstance(a, Mapping)
                )
            )
            if "cqrs" not in adr_text and "event sourcing" not in adr_text:
                objections.append(
                    f"CQRS-style components {cqrs_components} are present without a justifying "
                    "ADR (TITAN-WFL-026: CQRS is permitted only with an ADR)."
                )

        # Coverage — every TaskGraph task maps to an existing component (H5). Only
        # checkable when the SprintPlan is available; skip gracefully otherwise.
        plan = ctx.prior_artifacts.get("pmgr_plan")
        if plan is not None:
            mapping = {
                str(m["task_id"]): str(m["component"])
                for m in pkg.get("task_components", [])
                if isinstance(m, Mapping) and "task_id" in m and "component" in m
            }
            for task in plan.get("tasks", []):
                if not isinstance(task, Mapping):
                    continue
                tid = str(task.get("id"))
                if tid not in mapping:
                    objections.append(f"Task '{tid}' is not mapped to any package component.")
                elif mapping[tid] not in names:
                    objections.append(f"Task '{tid}' maps to unknown component '{mapping[tid]}'.")

        # OpenAPI 3.1 validity of every API contract's operation object.
        for contract in pkg.get("api_contracts", []):
            if not isinstance(contract, Mapping):
                continue
            operation = contract.get("operation", {})
            error = _operation_error(operation if isinstance(operation, Mapping) else {})
            if error is not None:
                objections.append(
                    f"API contract {contract.get('method')} {contract.get('path')}: operation is "
                    f"not a valid OpenAPI 3.1 operation object ({error})."
                )

        numbered = [f"{i}. {obj}" for i, obj in enumerate(objections, start=1)]
        return GateResult(
            passed=not numbered,
            reasons=numbered,
            evaluator_agent="code_reviewer",
            evaluator_model="rules",
            prompt_version="G5/v1",
            constitution_version=ctx.constitution_version,
        )


# --- registry ---------------------------------------------------------------

#: The producing stage each G2–G5 gate evaluates (G1 runs at clarification intake,
#: not on a producing stage, so it is not registered in the executor).
STANDARD_GATES: dict[str, Gate] = {
    "ceo_vision": ProjectBriefGate(),
    "pm_backlog": ProductBacklogGate(),
    "pmgr_plan": SprintPlanGate(),
    "architect_design": ArchitecturePackageGate(),
}

_INTAKE_GATE = UserRequestGate()


def register_standard_gates() -> None:
    """Register the concrete G2–G5 predicates into the executor's gate registry.
    Idempotent — safe to call at app/worker startup and in tests."""
    for stage, gate in STANDARD_GATES.items():
        register_gate(stage, gate)


def evaluate_intake_gate(raw_text: str, active_project_texts: Sequence[str]) -> GateResult:
    """Run G1 on a submitted UserRequest (H1). The clarification intake calls this;
    on failure it rejects at the API layer (G1's §2.3 failure path)."""
    ctx = GateContext(
        stage="user_request",
        body={"raw_text": raw_text},
        active_project_texts=tuple(active_project_texts),
    )
    return _INTAKE_GATE.evaluate(ctx)


__all__ = [
    "STANDARD_GATES",
    "ArchitecturePackageGate",
    "ProductBacklogGate",
    "ProjectBriefGate",
    "SprintPlanGate",
    "UserRequestGate",
    "evaluate_intake_gate",
    "register_standard_gates",
]
