"""The SHARED notification pipeline (Vol 7 §6.3, TITAN-INT-042). Backend-neutral so Slack
AND Discord use ONE routing/template/retry path — Discord does not build a second pipeline.

Three pure pieces:

* :class:`ChannelRouter` — per-project channel routing from an operator config map, with a
  default channel fallback.
* :class:`TemplateRenderer` — per-event-topic message templates (``str.format`` over the
  event payload), operator-configurable, with a generic fallback.
* :func:`send_with_retry` — exponential backoff (max 5 attempts); the FINAL failure is
  returned as a structured ``delivered=False`` result so the caller can surface it in the
  dashboard rather than lose it.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_ATTEMPTS = 5
_BASE_BACKOFF_S = 0.5


@dataclass
class ChannelRouter:
    """project_id → channel. ``routes`` is the operator map; ``default`` catches the rest."""

    routes: dict[str, str] = field(default_factory=dict)
    default: str = ""

    def channel_for(self, project_id: str, override: str = "") -> str:
        return override or self.routes.get(project_id, "") or self.default

    @classmethod
    def from_json(cls, raw: str, default: str = "") -> "ChannelRouter":
        import json

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        routes = (
            {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        )
        return cls(routes=routes, default=default)


@dataclass
class TemplateRenderer:
    """topic → template. Rendered with ``str.format(**payload)``; an unknown field or an
    untemplated topic falls back to a compact generic line so rendering never raises."""

    templates: dict[str, str] = field(default_factory=dict)

    def render(self, topic: str, payload: dict[str, Any]) -> str:
        template = self.templates.get(topic)
        if template is None:
            return self._generic(topic, payload)
        try:
            return template.format(**payload)
        except Exception:  # noqa: BLE001 — an operator template like {a[b]}/{a.b} must never
            # crash delivery (TypeError/AttributeError included); fall back to the generic line.
            return self._generic(topic, payload)

    @staticmethod
    def _generic(topic: str, payload: dict[str, Any]) -> str:
        fields = ", ".join(
            f"{k}={v}" for k, v in payload.items() if not isinstance(v, dict | list)
        )
        return f"[{topic}] {fields}" if fields else f"[{topic}]"

    @classmethod
    def from_json(cls, raw: str) -> "TemplateRenderer":
        import json

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        templates = (
            {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        )
        return cls(templates=templates)


@dataclass
class DeliveryResult:
    delivered: bool
    attempts: int
    detail: str = ""


def send_with_retry(
    send: Callable[[], bool],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> DeliveryResult:
    """Call ``send`` (returns True on success) up to ``max_attempts`` with exponential
    backoff (0.5s, 1s, 2s, …). A raise counts as a failed attempt. The final failure is a
    structured result — never an exception — so the caller surfaces it in the dashboard."""
    last = ""
    for attempt in range(1, max_attempts + 1):
        try:
            if send():
                return DeliveryResult(True, attempt)
            last = "send returned failure"
        except Exception as exc:  # noqa: BLE001 — a transport error is a retryable attempt
            last = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            sleep(_BASE_BACKOFF_S * (2 ** (attempt - 1)))
    return DeliveryResult(False, max_attempts, last)
