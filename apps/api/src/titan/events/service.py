"""Public service interface of the `events` module (TITAN-BCK-004).

Responsibility: outbox publisher, Redis Streams producers/consumers, WebSocket fan-out.
Owns: no tables.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
