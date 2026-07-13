"""The auto sprint planner (Vol 6 §4.1, TITAN-WFL-015). PURE — no I/O, no clock,
no DB — so it is exhaustively fixture-testable.

The Project Manager Agent partitions a TaskGraph into sprints such that:

* **sprint 1 is a walking skeleton** — a thin end-to-end slice touching each
  engineering role (frontend, backend, database) plus a deploy task;
* **DAG order holds** — no task lands in an earlier sprint than any of its
  dependencies (same sprint is allowed; strictly-earlier is not);
* **capacity is respected** — no sprint exceeds the configured parallel-agent
  capacity;
* **every sprint ends with a potentially releasable increment** — realised as
  "no sprint is empty", so each sprint strictly grows a deployable slice.

The planner is deliberately decoupled from the H4 ``SprintPlan`` handoff *artifact*
(``titan.handoffs.schemas``): planning is an independent domain and may not import
another domain's non-service internals. The executor that emits the H4 artifact can
call this planner through ``planning.service``.
"""

from collections import deque
from dataclasses import dataclass, field

#: The engineering roles a walking skeleton must cover (§4.2 M1 "frontend, backend,
#: database"). Configurable per call; this is the canonical default.
DEFAULT_ENGINEERING_ROLES: tuple[str, ...] = ("backend", "frontend", "database")
#: Roles that count as the sprint-1 deploy task (§4.2 M1 "deploy").
DEFAULT_DEPLOY_ROLES: tuple[str, ...] = ("devops", "deploy")

_SKELETON_GOAL = "Walking skeleton: a thin end-to-end slice (frontend, backend, database, deploy)."


class PlanningError(ValueError):
    """The backlog cannot be partitioned into a valid plan (a cycle, a missing
    skeleton role, or a skeleton that does not fit the capacity)."""


@dataclass(frozen=True)
class PlanTask:
    """One unscheduled task fed to the planner."""

    id: str
    role: str
    estimate_days: float = 0.5
    story_id: str = ""
    title: str = ""
    priority: int = 3


@dataclass(frozen=True)
class PlanEdge:
    """A ``task_id`` depends on ``depends_on_id`` (must precede or share its sprint)."""

    task_id: str
    depends_on_id: str


@dataclass(frozen=True)
class ScheduledTask:
    """A task with its assigned sprint number (1-based)."""

    id: str
    role: str
    sprint_number: int
    estimate_days: float
    story_id: str
    title: str
    priority: int


@dataclass(frozen=True)
class PlannedSprint:
    number: int
    goal: str
    task_ids: tuple[str, ...]
    walking_skeleton: bool


@dataclass(frozen=True)
class SprintPlan:
    sprints: tuple[PlannedSprint, ...] = field(default_factory=tuple)
    tasks: tuple[ScheduledTask, ...] = field(default_factory=tuple)


def _prereqs(edges: list[PlanEdge], ids: set[str]) -> dict[str, set[str]]:
    """Map each task id to the set of ids it depends on. Rejects edges that name an
    unknown task (a malformed graph)."""
    deps: dict[str, set[str]] = {tid: set() for tid in ids}
    for edge in edges:
        if edge.task_id not in ids or edge.depends_on_id not in ids:
            raise PlanningError(
                f"edge references unknown task: {edge.task_id} -> {edge.depends_on_id}"
            )
        if edge.task_id == edge.depends_on_id:
            raise PlanningError(f"self-dependency on task {edge.task_id}")
        deps[edge.task_id].add(edge.depends_on_id)
    return deps


def _assert_acyclic(deps: dict[str, set[str]]) -> None:
    """Kahn's algorithm; raises PlanningError naming the cyclic residue."""
    indeg = {tid: len(ds) for tid, ds in deps.items()}
    dependents: dict[str, list[str]] = {tid: [] for tid in deps}
    for tid, ds in deps.items():
        for d in ds:
            dependents[d].append(tid)
    queue = deque(sorted(tid for tid, n in indeg.items() if n == 0))
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for dep in sorted(dependents[node]):
            indeg[dep] -= 1
            if indeg[dep] == 0:
                queue.append(dep)
    if seen != len(deps):
        residue = sorted(tid for tid, n in indeg.items() if n > 0)
        raise PlanningError(f"dependency cycle among tasks: {residue}")


def _closure(task_id: str, deps: dict[str, set[str]]) -> set[str]:
    """A task plus all of its transitive prerequisites (a dependency-closed set)."""
    out: set[str] = set()
    stack = [task_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(deps[cur])
    return out


def _pick_skeleton(
    tasks: dict[str, PlanTask],
    deps: dict[str, set[str]],
    engineering_roles: tuple[str, ...],
    deploy_roles: tuple[str, ...],
) -> set[str]:
    """Choose the thinnest dependency-closed slice covering every engineering role
    plus a deploy task. For each required role we take the task with the smallest
    closure (tie-break: lower priority number, then id) and union the closures.

    This is a greedy, not globally-optimal, selector: on a pathological backlog with
    several tasks per role whose closures overlap poorly, the union can exceed
    capacity and ``plan_sprints`` will raise rather than search for a smaller covering
    slice. It never produces an INVALID plan (the invariants always hold when a plan
    is returned); it may over-conservatively reject such a backlog."""
    skeleton: set[str] = set()
    for role in (*engineering_roles, "<deploy>"):
        if role == "<deploy>":
            candidates = [t for t in tasks.values() if t.role in deploy_roles]
            label = f"a deploy task (roles {deploy_roles})"
        else:
            candidates = [t for t in tasks.values() if t.role == role]
            label = f"an engineering task with role '{role}'"
        if not candidates:
            raise PlanningError(f"walking skeleton needs {label}; none in the backlog")
        chosen = min(candidates, key=lambda t: (len(_closure(t.id, deps)), t.priority, t.id))
        skeleton |= _closure(chosen.id, deps)
    return skeleton


def plan_sprints(
    tasks: list[PlanTask],
    edges: list[PlanEdge],
    *,
    parallel_capacity: int,
    engineering_roles: tuple[str, ...] = DEFAULT_ENGINEERING_ROLES,
    deploy_roles: tuple[str, ...] = DEFAULT_DEPLOY_ROLES,
) -> SprintPlan:
    """Partition ``tasks`` into a walking-skeleton-first, DAG-respecting,
    capacity-bounded sprint plan (TITAN-WFL-015). Raises PlanningError on a cyclic,
    role-incomplete, or over-capacity backlog."""
    if parallel_capacity < 1:
        raise PlanningError("parallel_capacity must be >= 1")
    if not tasks:
        raise PlanningError("no tasks to plan")
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise PlanningError("duplicate task ids")
    deps = _prereqs(edges, set(by_id))
    _assert_acyclic(deps)

    skeleton = _pick_skeleton(by_id, deps, engineering_roles, deploy_roles)
    if len(skeleton) > parallel_capacity:
        raise PlanningError(
            f"walking skeleton ({len(skeleton)} tasks) exceeds parallel capacity "
            f"({parallel_capacity}); raise capacity or thin the skeleton"
        )

    sprint_of: dict[str, int] = {tid: 1 for tid in skeleton}
    remaining = sorted(
        (tid for tid in by_id if tid not in skeleton),
        key=lambda tid: (by_id[tid].priority, tid),
    )
    sprint_no = 1
    # Greedy list-scheduling: sprint n (n >= 2) takes up to `capacity` tasks whose
    # dependencies are ALL already placed in an earlier sprint.
    while remaining:
        sprint_no += 1
        eligible = [tid for tid in remaining if all(d in sprint_of for d in deps[tid])]
        if not eligible:  # unreachable given the acyclicity check, but fail loudly
            raise PlanningError(f"unschedulable residue (unresolved deps): {remaining}")
        chosen = eligible[:parallel_capacity]
        for tid in chosen:
            sprint_of[tid] = sprint_no
        remaining = [tid for tid in remaining if tid not in set(chosen)]

    sprints: list[PlannedSprint] = []
    for number in range(1, sprint_no + 1):
        members = tuple(sorted(tid for tid, s in sprint_of.items() if s == number))
        goal = (
            _SKELETON_GOAL if number == 1 else f"Sprint {number}: potentially releasable increment."
        )
        sprints.append(
            PlannedSprint(number=number, goal=goal, task_ids=members, walking_skeleton=number == 1)
        )
    scheduled = tuple(
        ScheduledTask(
            id=t.id,
            role=t.role,
            sprint_number=sprint_of[t.id],
            estimate_days=t.estimate_days,
            story_id=t.story_id,
            title=t.title,
            priority=t.priority,
        )
        for t in sorted(tasks, key=lambda t: (sprint_of[t.id], t.priority, t.id))
    )
    return SprintPlan(sprints=tuple(sprints), tasks=scheduled)


def verify_plan(
    plan: SprintPlan,
    edges: list[PlanEdge],
    *,
    parallel_capacity: int,
    engineering_roles: tuple[str, ...] = DEFAULT_ENGINEERING_ROLES,
    deploy_roles: tuple[str, ...] = DEFAULT_DEPLOY_ROLES,
) -> list[str]:
    """Independent re-check of the WFL-015 invariants. Returns a list of violation
    strings (empty ⇒ valid). Used by tests and as a defensive gate on any plan.

    Capacity/emptiness are checked against each sprint's ``task_ids`` and dependency
    order/role coverage against ``plan.tasks``; for a plan produced by
    :func:`plan_sprints` these two views are always consistent (built together)."""
    violations: list[str] = []
    sprint_of = {t.id: t.sprint_number for t in plan.tasks}

    for edge in edges:
        both_scheduled = edge.task_id in sprint_of and edge.depends_on_id in sprint_of
        if both_scheduled and sprint_of[edge.depends_on_id] > sprint_of[edge.task_id]:
            violations.append(
                f"{edge.task_id} (sprint {sprint_of[edge.task_id]}) precedes its dependency "
                f"{edge.depends_on_id} (sprint {sprint_of[edge.depends_on_id]})"
            )

    numbers = [s.number for s in plan.sprints]
    if numbers != list(range(1, len(numbers) + 1)):
        violations.append(f"sprint numbers are not contiguous from 1: {numbers}")
    for sprint in plan.sprints:
        if not sprint.task_ids:
            violations.append(f"sprint {sprint.number} is empty (no releasable increment)")
        if len(sprint.task_ids) > parallel_capacity:
            violations.append(
                f"sprint {sprint.number} has {len(sprint.task_ids)} tasks over capacity "
                f"{parallel_capacity}"
            )

    first = next((s for s in plan.sprints if s.number == 1), None)
    if first is None:
        violations.append("no sprint 1")
    else:
        if not first.walking_skeleton:
            violations.append("sprint 1 is not flagged as the walking skeleton")
        roles_in_first = {t.role for t in plan.tasks if t.sprint_number == 1}
        for role in engineering_roles:
            if role not in roles_in_first:
                violations.append(f"sprint 1 missing engineering role '{role}'")
        if not roles_in_first & set(deploy_roles):
            violations.append("sprint 1 has no deploy task")
    return violations


__all__ = [
    "DEFAULT_DEPLOY_ROLES",
    "DEFAULT_ENGINEERING_ROLES",
    "PlanEdge",
    "PlanTask",
    "PlannedSprint",
    "PlanningError",
    "ScheduledTask",
    "SprintPlan",
    "plan_sprints",
    "verify_plan",
]
