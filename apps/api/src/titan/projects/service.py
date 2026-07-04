"""Public service interface of the `projects` module (TITAN-BCK-004).

Responsibility: project lifecycle and roadmap data.
Owns: projects, milestones, sprints.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
