"""Public service interface of the `llm` module (TITAN-BCK-004).

Responsibility: provider-agnostic LLM gateway client with config-driven provider switching.
Owns: no tables.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""
