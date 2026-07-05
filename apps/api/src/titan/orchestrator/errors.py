"""Domain errors owned by the orchestrator module."""

import uuid

from titan.gateway.problems import ProblemException

PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE = "https://titan.dev/errors/invalid-dependency-cycle"
PROBLEM_TYPE_INVALID_TRANSITION = "https://titan.dev/errors/invalid-transition"


def invalid_transition(from_status: str, action: str) -> ProblemException:
    """409 for an illegal (from_status, action) pair (§4.3 / TITAN-BCK-012)."""
    return ProblemException(
        status=409,
        title="Invalid task transition",
        detail=f"Action '{action}' is not allowed from status '{from_status}'.",
        problem_type=PROBLEM_TYPE_INVALID_TRANSITION,
    )


class TransitionConflict(ProblemException):
    """The guarded status UPDATE moved zero rows: the task left its expected
    status between load and write (a concurrent transition won). Renders as 409
    invalid-transition if it reaches the gateway; the scheduler's claim path
    catches it and treats it as 'lost the race' (returns None)."""

    def __init__(self, from_status: str, action: str) -> None:
        super().__init__(
            status=409,
            title="Invalid task transition",
            detail=f"Task is no longer in status '{from_status}'; retry '{action}'.",
            problem_type=PROBLEM_TYPE_INVALID_TRANSITION,
        )


class DependencyCycleError(ProblemException):
    """Adding task_id → depends_on_id would create a cycle (TITAN-BCK-009).

    A :class:`ProblemException` subclass so the gateway's generic problem
    handler renders it as 409 ``invalid-dependency-cycle`` with no special-case
    wiring — the gateway (shared infra) never needs to import this domain
    module. ``status_code`` is kept as a convenience alias for callers that
    assert on it.
    """

    #: HTTP status; alias of ``status`` for call sites that read status_code.
    status_code = 409

    def __init__(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.depends_on_id = depends_on_id
        super().__init__(
            status=409,
            title="Invalid dependency cycle",
            detail=f"dependency {task_id} -> {depends_on_id} would create a cycle",
            problem_type=PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE,
        )
