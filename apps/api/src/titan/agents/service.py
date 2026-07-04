"""Public service interface of the `agents` module (TITAN-BCK-004).

Responsibility: agent registry: role, config, model binding, status.
Owns: agents.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
