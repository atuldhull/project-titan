"""The Linear connector entrypoint (Vol 7 §6.4, TITAN-INT-043/011) — the GraphQL sibling of the
Jira connector. Contributes its linear.* tools + the /hooks/linear webhook via the manifest,
reaching core ONLY through the capability handle (``secret`` for config, ``publish`` for task.*
events). Deleting this directory leaves core untouched (INT-011). Same honest scope as Jira: the
two-way ORCHESTRATION (mapping persistence + live TITAN task read) needs a core sync engine.
"""

import json
from typing import Any

from linear_api import DEFAULT_API_URL, LinearClient
from linear_events import translate
from linear_http import HttpTransport, UrllibTransport

_CONFIG_KEYS = (
    "LINEAR_API_KEY",
    "LINEAR_OAUTH_TOKEN",
    "LINEAR_API_URL",
    "LINEAR_TEAM_ID",
    "LINEAR_STATE_MAP",
)


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


class LinearConnector:
    id = "titan.linear"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        client: LinearClient | None = None,
    ) -> None:
        self._caps = capabilities
        cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        cfg = {k: v for k, v in cfg.items() if v}
        self.state_map: dict[str, str] = {}
        raw_map = cfg.get("LINEAR_STATE_MAP")
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    self.state_map = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                self.state_map = {}
        self._client = client or LinearClient(
            http or UrllibTransport(),
            api_url=cfg.get("LINEAR_API_URL") or DEFAULT_API_URL,
            api_key=cfg.get("LINEAR_API_KEY", ""),
            oauth_token=cfg.get("LINEAR_OAUTH_TOKEN", ""),
            team_id=cfg.get("LINEAR_TEAM_ID", ""),
        )

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        from linear_api import _ROUTES

        return sorted(_ROUTES)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(name, dict(arguments or {}))

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        published: list[str] = []
        for topic, event_payload in translate(body if isinstance(body, dict) else {}):
            if self._caps is not None:
                self._caps.publish(topic, event_payload)
            published.append(topic)
        return {"handled": True, "published": published}
