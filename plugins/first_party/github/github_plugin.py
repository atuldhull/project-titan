"""The GitHub connector entrypoint (Vol 7 §6.1, TITAN-INT-040/011).

A pure first-party plugin: it contributes its tools + webhook via the manifest and reaches
core ONLY through the capability handle (``secret`` for credentials/config, ``publish`` for
events). Deleting this directory leaves core untouched (INT-011). Construct it directly with
a fake capabilities + transport to unit-test the whole connector in-process.
"""

from typing import Any

from github_api import GithubClient
from github_auth import GithubAuth
from github_events import translate
from github_http import HttpTransport, UrllibTransport

DEFAULT_API_URL = "https://api.github.com"

#: Config carried on the secret channel (the only per-plugin operator-data path to the
#: subprocess in the Phase-52 runtime). Non-sensitive entries ride it too by necessity.
_CONFIG_KEYS = (
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_API_URL",
)


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


class GithubConnector:
    id = "titan.github"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        client: GithubClient | None = None,
    ) -> None:
        self._caps = capabilities
        self._cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        self._cfg = {k: v for k, v in self._cfg.items() if v}
        api_url = self._cfg.get("GITHUB_API_URL") or DEFAULT_API_URL
        transport = http or UrllibTransport()
        self._client = client or GithubClient(
            transport,
            GithubAuth(self._cfg, transport, api_url=api_url),
            api_url=api_url,
        )

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        # Advisory only; the manifest is the source of truth for what is registered.
        from github_api import _ROUTES

        return sorted(_ROUTES)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(name, dict(arguments or {}))

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        """Verified by core (X-Hub-Signature-256); translate the payload into vcs.* events
        and publish each onto the bus. Returns which topics were emitted."""
        events = translate(body if isinstance(body, dict) else {})
        published: list[str] = []
        for topic, event_payload in events:
            if self._caps is not None:
                self._caps.publish(topic, event_payload)
            published.append(topic)
        return {"handled": True, "published": published}
