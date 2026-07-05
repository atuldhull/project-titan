"""Domain errors owned by the orchestrator module."""

import uuid

from titan.gateway.problems import ProblemException

PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE = "https://titan.dev/errors/invalid-dependency-cycle"


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
