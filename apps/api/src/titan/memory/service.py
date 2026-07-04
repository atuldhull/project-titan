"""Public service interface of the `memory` module (TITAN-BCK-004).

Responsibility: memory API facade over PostgreSQL and Qdrant (internals: Vol 5).
Owns: memory_items and Qdrant collections.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
