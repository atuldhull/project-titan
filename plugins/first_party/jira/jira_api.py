"""The Jira REST v3 client (Vol 7 §6.4, TITAN-INT-043): the tracker I/O half of the sync.

Each TITAN task's issue carries a ``titan-task-<id>`` LABEL — that IS the per-issue half of the
mapping (so no separate mapping table is needed for outbound resolution: find_by_task does a JQL
label search). Auth: an API token (Basic email:token) or an OAuth bearer. Results use the
soft-error envelope ``{"ok": bool, ...}``.
"""

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode

from jira_http import HttpTransport

TASK_LABEL_PREFIX = "titan-task-"
DEFAULT_BASE_PLACEHOLDER = "https://your-domain.atlassian.net"


def task_label(task_id: str) -> str:
    return f"{TASK_LABEL_PREFIX}{task_id}"


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text as an Atlassian Document Format doc (Jira v3 requires ADF for bodies)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": str(text)}]}],
    }


class JiraClient:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        base_url: str,
        email: str = "",
        api_token: str = "",
        oauth_token: str = "",
    ) -> None:
        self._http = transport
        self._base = base_url.rstrip("/")
        self._email = email
        self._token = api_token
        self._oauth = oauth_token

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        builder = _ROUTES.get(tool)
        if builder is None:
            return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"no such tool {tool}"}
        try:
            method, path, body = builder(args)
        except KeyError as exc:
            return {"ok": False, "error_code": "BAD_ARGUMENT", "message": f"missing argument {exc}"}
        return self._request(method, path, body)

    def _auth_header(self) -> dict[str, str]:
        if self._oauth:
            return {"authorization": f"Bearer {self._oauth}"}
        if self._email and self._token:
            raw = base64.b64encode(f"{self._email}:{self._token}".encode()).decode("ascii")
            return {"authorization": f"Basic {raw}"}
        return {}

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        headers = {"accept": "application/json", "user-agent": "titan-jira-connector"}
        headers.update(self._auth_header())
        payload = json.dumps(body) if body is not None else None
        if payload is not None:
            headers["content-type"] = "application/json"
        resp = self._http.request(method, f"{self._base}{path}", headers=headers, body=payload)
        data = resp.json()
        if 200 <= resp.status < 300:
            return {"ok": True, "status": resp.status, "data": data}
        if resp.status == 429:
            retry = resp.headers.get("retry-after")
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": int(retry) if retry and retry.isdigit() else None,
                "message": "Jira rate limit hit",
            }
        msg = None
        if isinstance(data, dict):
            errs = data.get("errorMessages")
            msg = errs[0] if isinstance(errs, list) and errs else data.get("message")
        return {
            "ok": False,
            "error_code": "UPSTREAM_ERROR",
            "status": resp.status,
            "message": str(msg or "jira request failed"),
        }


def _seg(value: Any) -> str:
    return quote(str(value), safe="")


def _create_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    fields: dict[str, Any] = {
        "project": {"key": a["project_key"]},
        "summary": a["summary"],
        "issuetype": {"name": a.get("issue_type", "Task")},
        # The TITAN task id lives as a label — the durable per-issue side of the mapping.
        "labels": [task_label(a["task_id"])],
    }
    if a.get("description"):
        fields["description"] = _adf(a["description"])
    return "POST", "/rest/api/3/issue", {"fields": fields}


def _transition_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    # Jira transitions are by ID (a status NAME must first be resolved to a transition id via
    # GET /transitions — part of the orchestration). The tool takes the resolved transition_id.
    return (
        "POST",
        f"/rest/api/3/issue/{_seg(a['key'])}/transitions",
        {"transition": {"id": str(a["transition_id"])}},
    )


def _add_comment(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return "POST", f"/rest/api/3/issue/{_seg(a['key'])}/comment", {"body": _adf(a["body"])}


def _get_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return "GET", f"/rest/api/3/issue/{_seg(a['key'])}", None


def _find_by_task(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    jql = f'labels = "{task_label(a["task_id"])}"'
    return "GET", f"/rest/api/3/search?{urlencode({'jql': jql, 'maxResults': 2})}", None


_ROUTES = {
    "jira.create_issue": _create_issue,
    "jira.transition_issue": _transition_issue,
    "jira.add_comment": _add_comment,
    "jira.get_issue": _get_issue,
    "jira.find_by_task": _find_by_task,
}
