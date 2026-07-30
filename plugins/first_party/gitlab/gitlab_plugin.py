"""The GitLab connector entrypoint (Vol 7 §6.2, TITAN-INT-041/011).

A pure first-party plugin: contributes its gitlab.* + vcs.* tools and its webhook via the
manifest, reaching core ONLY through the capability handle (``secret`` for credentials/config,
``publish`` for events). Deleting this directory leaves core untouched (INT-011). Construct it
directly with a fake capabilities + transport to unit-test the whole connector in-process.
"""

from typing import Any

from gitlab_api import DEFAULT_API_URL, GitlabClient
from gitlab_events import translate
from gitlab_http import HttpTransport, UrllibTransport

_CONFIG_KEYS = ("GITLAB_TOKEN", "GITLAB_API_URL")


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


class GitlabConnector:
    id = "titan.gitlab"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        client: GitlabClient | None = None,
    ) -> None:
        self._caps = capabilities
        cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        cfg = {k: v for k, v in cfg.items() if v}
        api_url = cfg.get("GITLAB_API_URL") or DEFAULT_API_URL
        self._client = client or GitlabClient(
            http or UrllibTransport(), api_url=api_url, token=cfg.get("GITLAB_TOKEN", "")
        )

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        from gitlab_api import _ROUTES

        return sorted(_ROUTES)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(name, dict(arguments or {}))

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        """Called by core AFTER it verifies the webhook signature; translate the GitLab payload
        into vcs.* events and publish each. Returns which topics were emitted."""
        published: list[str] = []
        for topic, event_payload in translate(body if isinstance(body, dict) else {}):
            if self._caps is not None:
                self._caps.publish(topic, event_payload)
            published.append(topic)
        return {"handled": True, "published": published}
