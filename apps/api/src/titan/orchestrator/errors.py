"""Domain errors owned by the orchestrator module."""

import uuid

PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE = "https://titan.dev/errors/invalid-dependency-cycle"


class DependencyCycleError(Exception):
    """Adding task_id → depends_on_id would create a cycle (TITAN-BCK-009).

    Maps to HTTP 409 with problem type
    ``https://titan.dev/errors/invalid-dependency-cycle`` once the REST layer
    lands (Phase 11).
    """

    problem_type = PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE
    status_code = 409

    def __init__(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.depends_on_id = depends_on_id
        super().__init__(f"dependency {task_id} -> {depends_on_id} would create a cycle")
