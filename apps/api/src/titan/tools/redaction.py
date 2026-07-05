"""Pluggable secret redaction for tool arguments and results (§4.2 step 6,
TITAN-INT-023 "secrets MUST be redacted before persistence", Vol 8 §6.4).

The default redactor is deliberately aggressive: it masks by KEY (any mapping
key that looks credential-like) and by VALUE (token-shaped strings anywhere,
including inside prose), because audit rows and tool_result blocks both outlive
the call — one feeds Grafana/Loki, the other feeds the next LLM prompt. A
custom hook (``Redactor``) can replace it wholesale on the bus.
"""

import re
from collections.abc import Callable
from typing import Any

Redactor = Callable[[Any], Any]

MASK = "[REDACTED]"

_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|passwd|password|authorization|credential"
    r"|private[_-]?key|access[_-]?key|session[_-]?id|cookie)"
)

# Well-known credential shapes (mirrors the cassette scrubber + gitleaks rules).
_SECRET_VALUES = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic-style keys
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*"),  # Authorization values
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWTs
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"postgresql(?:\+\w+)?://[^:/\s]+:[^@\s]+@"),  # DSN with password
]


def _redact_text(value: str) -> str:
    for pattern in _SECRET_VALUES:
        value = pattern.sub(MASK, value)
    return value


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
