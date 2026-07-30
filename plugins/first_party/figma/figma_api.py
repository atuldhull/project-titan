"""The Figma REST client (Vol 7 §6.5, TITAN-INT-044): builds the request for each read tool,
applies the token, and returns a STRUCTURED soft-error envelope ``{"ok": bool, ...}`` — a
rate-limited (429) or failed read returns ``ok=False, error_code=RATE_LIMITED|UPSTREAM_ERROR``
(with ``retry_after`` for rate limits) so the caller can reschedule rather than only seeing a
generic error. A genuine transport crash still raises (→ bus UPSTREAM_ERROR).

Auth: a personal access token rides ``X-Figma-Token``; an OAuth bearer rides ``Authorization``.
All tools are read-only (``risk: read``), so there is no pre-emptive write throttle.
"""

from typing import Any
from urllib.parse import quote, urlencode

from figma_http import HttpTransport

DEFAULT_API_URL = "https://api.figma.com"


class FigmaClient:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_url: str,
        token: str = "",
        oauth_token: str = "",
    ) -> None:
        self._http = transport
        self._api = api_url.rstrip("/")
        self._token = token
        self._oauth = oauth_token

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        builder = _ROUTES.get(tool)
        if builder is None:
            return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"no such tool {tool}"}
        try:
            method, path = builder(args)
        except KeyError as exc:
            return {"ok": False, "error_code": "BAD_ARGUMENT", "message": f"missing argument {exc}"}
        return self._request(method, path)

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": "titan-figma-connector"}
        # OAuth bearer takes precedence; else the personal access token.
        if self._oauth:
            headers["authorization"] = f"Bearer {self._oauth}"
        elif self._token:
            headers["x-figma-token"] = self._token
        return headers

    def _request(self, method: str, path: str) -> dict[str, Any]:
        resp = self._http.request(method, f"{self._api}{path}", headers=self._headers(), body=None)
        data = resp.json()
        if 200 <= resp.status < 300:
            return {"ok": True, "status": resp.status, "data": data}
        if resp.status == 429:
            retry = resp.headers.get("retry-after")
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": int(retry) if retry and retry.isdigit() else None,
                "message": "Figma rate limit hit",
            }
        message = data.get("err") or data.get("message") if isinstance(data, dict) else None
        return {
            "ok": False,
            "error_code": "UPSTREAM_ERROR",
            "status": resp.status,
            "message": str(message or "figma request failed"),
        }


def _seg(value: Any) -> str:
    """Percent-encode ONE path segment so a file_key/node_id argument cannot inject ``/``,
    ``?`` or ``..`` and reach a different figma resource than the audited call named."""
    return quote(str(value), safe="")


def _get_file(a: dict[str, Any]) -> tuple[str, str]:
    path = f"/v1/files/{_seg(a['file_key'])}"
    params = {}
    if a.get("depth"):
        params["depth"] = str(int(a["depth"]))
    if a.get("ids"):
        params["ids"] = _ids(a["ids"])
    return "GET", path + (f"?{urlencode(params)}" if params else "")


def _get_node_image(a: dict[str, Any]) -> tuple[str, str]:
    params = {
        "ids": _ids(a["ids"]),
        "format": str(a.get("format", "png")),
        "scale": str(a.get("scale", 1)),
    }
    return "GET", f"/v1/images/{_seg(a['file_key'])}?{urlencode(params)}"


def _get_variables(a: dict[str, Any]) -> tuple[str, str]:
    return "GET", f"/v1/files/{_seg(a['file_key'])}/variables/local"


def _get_comments(a: dict[str, Any]) -> tuple[str, str]:
    return "GET", f"/v1/files/{_seg(a['file_key'])}/comments"


def _ids(value: Any) -> str:
    """Figma's ``ids`` query wants a comma-joined list; accept a list or a pre-joined string."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


_ROUTES = {
    "figma.get_file": _get_file,
    "figma.get_node_image": _get_node_image,
    "figma.get_variables": _get_variables,
    "figma.get_comments": _get_comments,
}
