"""Public service interface of the `gateway` module (TITAN-BCK-004).

Responsibility: routing, authn/z dependencies, rate limits, request IDs, CORS, OpenAPI wiring.
Owns: no tables.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
