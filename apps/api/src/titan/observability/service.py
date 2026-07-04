"""Public service interface of the `observability` module (TITAN-BCK-004).

Responsibility: OpenTelemetry setup, structlog configuration, Prometheus metrics registry.
Owns: no tables.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
