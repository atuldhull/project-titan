"""Public service interface of the `identity` module (TITAN-BCK-004).

Responsibility: OAuth2/OIDC flows, JWT issuing/verification, RBAC checks.
Owns: users, roles, user_roles, api_keys.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
