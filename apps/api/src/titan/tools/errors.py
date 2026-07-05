"""The Tool Bus error taxonomy (Vol 7 §4.2).

Failures surface as a ``status: "error"`` result envelope carrying a
machine-readable ``code`` plus an LLM-readable ``message`` (schema-pointer text
for validation errors), so the calling agent can self-correct (Vol 6 retry
policy). Codes are a closed set — executors and constraints raise
:class:`ToolError` with one of them; anything else a tool raises is folded into
``UPSTREAM_ERROR`` by the bus.
"""

from typing import Literal, get_args

ToolErrorCode = Literal[
    "SCHEMA_INVALID",
    "PERMISSION_DENIED",
    "TIMEOUT",
    "SANDBOX_VIOLATION",
    "UPSTREAM_ERROR",
    "TOOL_UNAVAILABLE",
]

TOOL_ERROR_CODES: frozenset[str] = frozenset(get_args(ToolErrorCode))


class ToolError(Exception):
    """A tool-call failure with a taxonomy code, rendered into the result
    envelope by the bus (never propagated to the caller as an exception)."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ToolErrorCode = code
        self.message = message


__all__ = ["TOOL_ERROR_CODES", "ToolError", "ToolErrorCode"]
