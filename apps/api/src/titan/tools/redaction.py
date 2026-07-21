"""Pluggable secret redaction for tool arguments and results (§4.2 step 6,
TITAN-INT-023 "secrets MUST be redacted before persistence", Vol 8 §6.4).

The default redactor is deliberately aggressive: it masks by KEY (any mapping
key that looks credential-like) and by VALUE — since Phase 69 the value pass
is the shared :mod:`titan.redaction` engine (TITAN-SEC-054: live-inventory
hashed lookup + provider-key patterns + high-entropy tokens), because audit
rows and tool_result blocks both outlive the call — one feeds Grafana/Loki,
the other feeds the next LLM prompt. Key-name masking stays here: it is a
structure-level heuristic the text engine cannot express. A custom hook
(``Redactor``) can replace it wholesale on the bus.
"""

import re
from collections.abc import Callable
from typing import Any

from titan.redaction import default_engine

Redactor = Callable[[Any], Any]

MASK = "[REDACTED]"

_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|passwd|password|authorization|credential"
    r"|private[_-]?key|access[_-]?key|session[_-]?id|cookie)"
)


def _redact_text(value: str) -> str:
    return default_engine().redact(value, sink="tool")


def default_redactor(value: Any) -> Any:
    """Recursively mask credential-like keys and token-shaped values. Returns
    a new structure; never mutates the input."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            key: (MASK if isinstance(key, str) and _SECRET_KEY.search(key) else default_redactor(v))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [default_redactor(item) for item in value]
    return value


__all__ = ["MASK", "Redactor", "default_redactor"]
