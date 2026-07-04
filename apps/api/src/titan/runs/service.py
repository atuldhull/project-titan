"""Public service interface of the `runs` module (TITAN-BCK-004).

Responsibility: execution records of agent work and artifact metadata.
Owns: runs, artifacts.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
