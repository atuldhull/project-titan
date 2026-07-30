"""The Figma connector entrypoint (Vol 7 §6.5, TITAN-INT-044/011).

A pure first-party READ plugin: it contributes its 4 read tools via the manifest and reaches
core ONLY through the capability handle (``secret`` for the token/config). No webhook — Figma
is a pull-only source. Deleting this directory leaves core untouched (INT-011). Construct it
directly with a fake capabilities + transport to unit-test the whole connector in-process.
"""

from typing import Any

from figma_api import DEFAULT_API_URL, FigmaClient
from figma_http import HttpTransport, UrllibTransport
from figma_tokens import to_tailwind_config

_CONFIG_KEYS = ("FIGMA_TOKEN", "FIGMA_OAUTH_TOKEN", "FIGMA_API_URL")


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


class FigmaConnector:
    id = "titan.figma"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        client: FigmaClient | None = None,
    ) -> None:
        self._caps = capabilities
        cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        cfg = {k: v for k, v in cfg.items() if v}
        api_url = cfg.get("FIGMA_API_URL") or DEFAULT_API_URL
        self._client = client or FigmaClient(
            http or UrllibTransport(),
            api_url=api_url,
            token=cfg.get("FIGMA_TOKEN", ""),
            oauth_token=cfg.get("FIGMA_OAUTH_TOKEN", ""),
        )

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        # Advisory only; the manifest is the source of truth for what is registered.
        from figma_api import _ROUTES

        return sorted(_ROUTES)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(name, dict(arguments or {}))

    def tailwind_config_from_variables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Convenience for the Frontend Engineer agent (INT-044): fetch a file's variables and
        render a tailwind.config.ts. Returns the soft-error envelope on a failed fetch."""
        result = self._client.call("figma.get_variables", dict(arguments or {}))
        if not result.get("ok"):
            return result
        return {"ok": True, "tailwind_config": to_tailwind_config(result.get("data") or {})}
