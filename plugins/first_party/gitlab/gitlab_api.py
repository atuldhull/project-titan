"""The GitLab REST client (Vol 7 §6.2, TITAN-INT-041): mirrors the GitHub reference mapped to
GitLab semantics (merge requests, pipelines). Each contributed tool — the provider-specific
``gitlab.*`` AND the generic ``vcs.*`` aliases — routes to the same request builder, so a
Volume-6 workflow written against ``vcs.open_change_request`` / ``vcs.merge`` runs unchanged
with GitLab as the only VCS connector (INT-041).

Results use the soft-error envelope ``{"ok": bool, ...}``: a rate-limited (429) or failed write
returns ``ok=False, error_code=RATE_LIMITED|UPSTREAM_ERROR`` (with ``retry_after`` for rate
limits) so the orchestrator reschedules; a genuine transport crash still raises.
"""

from typing import Any
from urllib.parse import quote, urlencode

from gitlab_http import HttpTransport

DEFAULT_API_URL = "https://gitlab.com/api/v4"


class GitlabClient:
    def __init__(self, transport: HttpTransport, *, api_url: str, token: str = "") -> None:
        self._http = transport
        self._api = api_url.rstrip("/")
        self._token = token

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        builder = _ROUTES.get(tool)
        if builder is None:
            return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"no such tool {tool}"}
        try:
            method, path, body = builder(args)
        except KeyError as exc:
            return {"ok": False, "error_code": "BAD_ARGUMENT", "message": f"missing argument {exc}"}
        return self._request(method, path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        import json

        headers = {"accept": "application/json", "user-agent": "titan-gitlab-connector"}
        if self._token:
            headers["private-token"] = self._token
        payload = json.dumps(body) if body is not None else None
        if payload is not None:
            headers["content-type"] = "application/json"
        resp = self._http.request(method, f"{self._api}{path}", headers=headers, body=payload)
        data = resp.json()
        if 200 <= resp.status < 300:
            return {"ok": True, "status": resp.status, "data": data}
        if resp.status == 429:
            retry = resp.headers.get("retry-after")
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": int(retry) if retry and retry.isdigit() else None,
                "message": "GitLab rate limit hit",
            }
        message = data.get("message") or data.get("error") if isinstance(data, dict) else None
        return {
            "ok": False,
            "error_code": "UPSTREAM_ERROR",
            "status": resp.status,
            "message": str(message or "gitlab request failed"),
        }


def _seg(value: Any) -> str:
    """Percent-encode ONE segment so a project path (group/project → group%2Fproject) or an
    iid/ref/branch cannot inject ``/``/``?``/``..`` and reach a different gitlab.com resource."""
    return quote(str(value), safe="")


def _proj(a: dict[str, Any]) -> str:
    return _seg(a["project"])


def _create_branch(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    q = urlencode({"branch": a["branch"], "ref": a["ref"]})
    return "POST", f"/projects/{_proj(a)}/repository/branches?{q}", None


def _open_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {
        "source_branch": a["source_branch"],
        "target_branch": a["target_branch"],
        "title": a["title"],
    }
    if a.get("description"):
        body["description"] = a["description"]
    return "POST", f"/projects/{_proj(a)}/merge_requests", body


def _update_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body = {k: a[k] for k in ("title", "description", "state_event", "target_branch") if k in a}
    return "PUT", f"/projects/{_proj(a)}/merge_requests/{_seg(a['iid'])}", body


def _approve_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return "POST", f"/projects/{_proj(a)}/merge_requests/{_seg(a['iid'])}/approve", None


def _comment_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return (
        "POST",
        f"/projects/{_proj(a)}/merge_requests/{_seg(a['iid'])}/notes",
        {"body": a["body"]},
    )


def _review_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    # The vcs.review alias: 'approve' hits the approval endpoint; anything else is a review note
    # (GitLab has no single 'submit review' call, so review = approve and/or comment).
    if str(a.get("event", "")).lower() == "approve":
        return _approve_mr(a)
    return _comment_mr(a)


def _merge_mr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {}
    if a.get("squash") is not None:
        body["squash"] = bool(a["squash"])
    if a.get("merge_commit_message"):
        body["merge_commit_message"] = a["merge_commit_message"]
    return "PUT", f"/projects/{_proj(a)}/merge_requests/{_seg(a['iid'])}/merge", body


def _create_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {"title": a["title"]}
    if a.get("description"):
        body["description"] = a["description"]
    if a.get("labels"):
        labels = a["labels"]
        body["labels"] = ",".join(labels) if isinstance(labels, (list, tuple)) else str(labels)
    return "POST", f"/projects/{_proj(a)}/issues", body


def _comment_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return "POST", f"/projects/{_proj(a)}/issues/{_seg(a['iid'])}/notes", {"body": a["body"]}


def _get_pipelines(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    # A commit's statuses are the closest analog to GitHub check-runs; else list pipelines by ref.
    if a.get("sha"):
        return "GET", f"/projects/{_proj(a)}/repository/commits/{_seg(a['sha'])}/statuses", None
    q = urlencode({"ref": a["ref"]}) if a.get("ref") else ""
    return "GET", f"/projects/{_proj(a)}/pipelines" + (f"?{q}" if q else ""), None


def _search(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    q = urlencode({"scope": a.get("scope", "blobs"), "search": a["q"]})
    return "GET", f"/projects/{_proj(a)}/search?{q}", None


# Both the provider-specific name and its vcs.* alias route to ONE builder (INT-041).
_ROUTES = {
    "gitlab.create_branch": _create_branch,
    "gitlab.open_mr": _open_mr,
    "gitlab.update_mr": _update_mr,
    "gitlab.approve_mr": _approve_mr,
    "gitlab.comment_mr": _comment_mr,
    "gitlab.merge_mr": _merge_mr,
    "gitlab.create_issue": _create_issue,
    "gitlab.comment_issue": _comment_issue,
    "gitlab.get_pipelines": _get_pipelines,
    "gitlab.search": _search,
    "vcs.create_branch": _create_branch,
    "vcs.open_change_request": _open_mr,
    "vcs.update_change_request": _update_mr,
    "vcs.review": _review_mr,
    "vcs.comment": _comment_mr,
    "vcs.merge": _merge_mr,
    "vcs.create_issue": _create_issue,
    "vcs.get_checks": _get_pipelines,
}
