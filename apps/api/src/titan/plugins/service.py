"""Public service interface of the `plugins` module (TITAN-BCK-004).

Responsibility: plugin registry, manifest validation, lifecycle hooks (Vol 7).
Owns: plugins, plugin_installs.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
