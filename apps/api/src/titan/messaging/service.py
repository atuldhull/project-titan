"""Public service interface of the `messaging` module (TITAN-BCK-004).

Responsibility: structured inter-agent messages and the decision log.
Owns: messages, decisions.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
