"""The Jira connector entrypoint (Vol 7 §6.4, TITAN-INT-043/011).

A pure first-party plugin: contributes its jira.* tools + the /hooks/jira webhook via the
manifest, reaching core ONLY through the capability handle (``secret`` for credentials/config,
``publish`` for task.* events). Deleting this directory leaves core untouched (INT-011).

Scope note (honest): this connector owns the tracker I/O (jira.* tools), the jira:issue_updated
→ task.* translation, and the sync ALGORITHM (jira_sync.resolve_status: state map + the
sync_conflict rule). The two-way ORCHESTRATION — persisting the per-project mapping/last-synced
marker and comparing live TITAN task state — needs a core sync engine or extra plugin
capabilities (task-read + storage) the connector lacks; see README.md.
"""

import json
from typing import Any

from jira_api import DEFAULT_BASE_PLACEHOLDER, JiraClient
from jira_events import translate
from jira_http import HttpTransport, UrllibTransport

_CONFIG_KEYS = (
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_OAUTH_TOKEN",
    "JIRA_STATE_MAP",
)


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


class JiraConnector:
    id = "titan.jira"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        client: JiraClient | None = None,
    ) -> None:
        self._caps = capabilities
        cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        cfg = {k: v for k, v in cfg.items() if v}
        # The operator-configured TITAN-state → Jira-status map (config_schema, on the secret
        # channel as JSON); available to the sync engine that drives resolve_status.
        self.state_map: dict[str, str] = {}
        raw_map = cfg.get("JIRA_STATE_MAP")
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    self.state_map = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                self.state_map = {}
        self._client = client or JiraClient(
            http or UrllibTransport(),
            base_url=cfg.get("JIRA_BASE_URL") or DEFAULT_BASE_PLACEHOLDER,
            email=cfg.get("JIRA_EMAIL", ""),
            api_token=cfg.get("JIRA_API_TOKEN", ""),
            oauth_token=cfg.get("JIRA_OAUTH_TOKEN", ""),
        )

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        from jira_api import _ROUTES

        return sorted(_ROUTES)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(name, dict(arguments or {}))

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        """Called by core AFTER it verifies the webhook signature; translate the Jira payload
        into task.* events and publish each. Returns which topics were emitted."""
        published: list[str] = []
        for topic, event_payload in translate(body if isinstance(body, dict) else {}):
            if self._caps is not None:
                self._caps.publish(topic, event_payload)
            published.append(topic)
        return {"handled": True, "published": published}
