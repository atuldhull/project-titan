"""The Linear GraphQL client (Vol 7 §6.4, TITAN-INT-043): the tracker I/O half, GraphQL variant
of the Jira connector. Every tool POSTs ``{query, variables}`` to ``/graphql``.

Each issue embeds a ``[titan-task:<id>]`` MARKER in its description — the durable per-issue side
of the mapping (find_by_task filters issues whose description contains it, so no separate mapping
table is needed for outbound resolution). Auth: a personal API key (``Authorization: <key>``) or
an OAuth bearer. GraphQL returns errors as HTTP 200 with an ``errors`` array — folded into the
soft-error envelope ``{"ok": bool, ...}`` like any upstream failure.
"""

import json
from typing import Any

from linear_http import HttpTransport

DEFAULT_API_URL = "https://api.linear.app"


def task_marker(task_id: str) -> str:
    return f"titan-task:{task_id}"


def _with_marker(description: str, task_id: str) -> str:
    return f"{description}\n\n[{task_marker(task_id)}]"


_CREATE = (
    "mutation($input: IssueCreateInput!){issueCreate(input:$input){success issue{id identifier}}}"
)
_UPDATE = (
    "mutation($id: String!, $input: IssueUpdateInput!)"
    "{issueUpdate(id:$id,input:$input){success issue{id}}}"
)
_COMMENT = "mutation($input: CommentCreateInput!){commentCreate(input:$input){success comment{id}}}"
_GET = "query($id: String!){issue(id:$id){id identifier title description state{name}}}"
_FIND = "query($m: String!){issues(filter:{description:{contains:$m}}){nodes{id identifier}}}"


class LinearClient:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_url: str,
        api_key: str = "",
        oauth_token: str = "",
        team_id: str = "",
    ) -> None:
        self._http = transport
        self._api = api_url.rstrip("/")
        self._key = api_key
        self._oauth = oauth_token
        self._team = team_id

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        builder = _ROUTES.get(tool)
        if builder is None:
            return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"no such tool {tool}"}
        try:
            query, variables = builder(self, args)
        except KeyError as exc:
            return {"ok": False, "error_code": "BAD_ARGUMENT", "message": f"missing argument {exc}"}
        return self._request(query, variables)

    def _auth(self) -> str:
        # OAuth bearer takes precedence; a Linear personal API key goes in Authorization verbatim.
        if self._oauth:
            return f"Bearer {self._oauth}"
        return self._key

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "titan-linear-connector",
        }
        auth = self._auth()
        if auth:
            headers["authorization"] = auth
        body = json.dumps({"query": query, "variables": variables})
        resp = self._http.request("POST", f"{self._api}/graphql", headers=headers, body=body)
        data = resp.json()
        if resp.status == 429:
            retry = resp.headers.get("retry-after")
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": int(retry) if retry and retry.isdigit() else None,
                "message": "Linear rate limit hit",
            }
        if not (200 <= resp.status < 300):
            return {
                "ok": False,
                "error_code": "UPSTREAM_ERROR",
                "status": resp.status,
                "message": "linear request failed",
            }
        # A GraphQL error is HTTP 200 with a non-empty `errors` array.
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            msg = errors[0].get("message") if isinstance(errors[0], dict) else str(errors[0])
            return {"ok": False, "error_code": "UPSTREAM_ERROR", "status": 200, "message": str(msg)}
        return {"ok": True, "status": resp.status, "data": data.get("data", data)}


def _create(client: LinearClient, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    inp: dict[str, Any] = {
        "teamId": a.get("team_id") or client._team,  # noqa: SLF001 — same-connector helper
        "title": a["title"],
        "description": _with_marker(a.get("description", ""), a["task_id"]),
    }
    if a.get("state_id"):
        inp["stateId"] = a["state_id"]
    return _CREATE, {"input": inp}


def _update(client: LinearClient, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    inp = {k: a[k] for k in ("title", "description") if k in a}
    if a.get("state_id"):
        inp["stateId"] = a["state_id"]  # a state transition = a stateId update
    return _UPDATE, {"id": a["id"], "input": inp}


def _comment(client: LinearClient, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _COMMENT, {"input": {"issueId": a["id"], "body": a["body"]}}


def _get(client: LinearClient, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _GET, {"id": a["id"]}


def _find(client: LinearClient, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _FIND, {"m": task_marker(a["task_id"])}


_ROUTES = {
    "linear.create_issue": _create,
    "linear.update_issue": _update,
    "linear.add_comment": _comment,
    "linear.get_issue": _get,
    "linear.find_by_task": _find,
}
