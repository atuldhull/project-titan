"""Public service interface of the `orchestrator` module (TITAN-BCK-004).

Responsibility: task state machine, assignment, prioritization, scheduling.
Owns: tasks, task_dependencies.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
