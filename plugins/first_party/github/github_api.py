"""The GitHub REST client (Vol 7 §6.1, TITAN-INT-040): builds the request for each
contributed tool, applies the resolved auth token, folds every response into the rate
bucket, and returns a STRUCTURED result.

Tool results use a soft-error envelope ``{"ok": bool, ...}`` because a plugin tool returns
a value, not a bus error code: a rate-limited or failed write returns
``ok=False, error_code=RATE_LIMITED|UPSTREAM_ERROR`` (with ``retry_after`` for rate limits)
so the orchestrator can reschedule with the wait hint intact rather than only seeing a
generic UPSTREAM_ERROR. A genuine transport crash still raises (→ bus UPSTREAM_ERROR).
"""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from github_auth import GithubAuth
from github_http import HttpTransport
from github_ratelimit import RateBucket

API_VERSION = "2022-11-28"

#: Which contributed tools mutate — only these are pre-emptively rate-limited (§6.1).
WRITE_TOOLS = frozenset(
    {
        "github.create_repo",
        "github.create_branch",
        "github.open_pr",
        "github.update_pr",
        "github.review_pr",
        "github.merge_pr",
        "github.create_issue",
        "github.comment",
    }
)


class GithubClient:
    def __init__(
        self,
        transport: HttpTransport,
        auth: GithubAuth,
        *,
        api_url: str,
        bucket: RateBucket | None = None,
        clock: "Callable[[], float]" = time.time,
    ) -> None:
        self._http = transport
        self._auth = auth
        self._api = api_url.rstrip("/")
        self.bucket = bucket or RateBucket()
        self._clock = clock

    # --- tool dispatch ------------------------------------------------------

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # A write refused up front once the budget is < 10% (§6.1): report RATE_LIMITED +
        # retry_after so the orchestrator reschedules instead of forcing a hard 403.
        if tool in WRITE_TOOLS and self.bucket.should_throttle():
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": self.bucket.retry_after(now_epoch=int(self._clock())),
                "message": "GitHub rate budget below 10%; rescheduling",
            }
        builder = _ROUTES.get(tool)
        if builder is None:
            return {
                "ok": False,
                "error_code": "UNKNOWN_TOOL",
                "message": f"no such tool {tool}",
            }
        method, path, body = builder(args)
        return self._request(method, path, body)

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        import json

        headers = {
            "authorization": f"Bearer {self._auth.token()}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": API_VERSION,
            "user-agent": "titan-github-connector",
        }
        payload = json.dumps(body) if body is not None else None
        if payload is not None:
            headers["content-type"] = "application/json"
        resp = self._http.request(
            method, f"{self._api}{path}", headers=headers, body=payload
        )
        self.bucket.observe(resp.headers)
        data = resp.json()
        if 200 <= resp.status < 300:
            return {"ok": True, "status": resp.status, "data": data}
        # A primary OR secondary/abuse rate limit (403/429). Secondary limits can leave
        # remaining > 0 but carry a Retry-After header — honor it; else the primary window.
        retry_after_hdr = _int_or_none(resp.headers.get("retry-after"))
        if resp.status in (403, 429) and (
            retry_after_hdr is not None or self.bucket.remaining == 0
        ):
            return {
                "ok": False,
                "error_code": "RATE_LIMITED",
                "retry_after": retry_after_hdr
                if retry_after_hdr is not None
                else self.bucket.retry_after(now_epoch=int(self._clock())),
                "message": "GitHub rate limit hit",
            }
        return {
            "ok": False,
            "error_code": "UPSTREAM_ERROR",
            "status": resp.status,
            "message": str(data.get("message", "github request failed"))
            if isinstance(data, dict)
            else "github request failed",
        }


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# --- per-tool request builders: tool -> (method, path, body) ----------------

_Builder = "Callable[[dict[str, Any]], tuple[str, str, dict[str, Any] | None]]"


def _seg(value: Any) -> str:
    """Percent-encode ONE path segment so a tool argument (owner/repo/number/ref/sha/branch)
    cannot inject ``/``, ``?``, ``#`` or ``..`` and reach a different api.github.com resource
    than the audited call named."""
    return quote(str(value), safe="")


def _repo(args: dict[str, Any]) -> str:
    return f"{_seg(args['owner'])}/{_seg(args['repo'])}"


def _create_repo(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body = {"name": a["name"], "private": bool(a.get("private", True))}
    if a.get("description"):
        body["description"] = a["description"]
    path = f"/orgs/{_seg(a['org'])}/repos" if a.get("org") else "/user/repos"
    return "POST", path, body


def _create_branch(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    # Create a ref from a base sha (branch → the first step of the PR lifecycle).
    return (
        "POST",
        f"/repos/{_repo(a)}/git/refs",
        {"ref": f"refs/heads/{a['branch']}", "sha": a["sha"]},
    )


def _open_pr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body = {"title": a["title"], "head": a["head"], "base": a["base"]}
    if a.get("body"):
        body["body"] = a["body"]
    if a.get("draft"):
        body["draft"] = bool(a["draft"])
    return "POST", f"/repos/{_repo(a)}/pulls", body


def _update_pr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body = {k: a[k] for k in ("title", "body", "state", "base") if k in a}
    return "PATCH", f"/repos/{_repo(a)}/pulls/{_seg(a['number'])}", body


def _review_pr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {"event": a.get("event", "COMMENT")}
    if a.get("body"):
        body["body"] = a["body"]
    if a.get("comments"):
        body["comments"] = a["comments"]
    return "POST", f"/repos/{_repo(a)}/pulls/{_seg(a['number'])}/reviews", body


def _merge_pr(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {"merge_method": a.get("merge_method", "squash")}
    if a.get("commit_title"):
        body["commit_title"] = a["commit_title"]
    return "PUT", f"/repos/{_repo(a)}/pulls/{_seg(a['number'])}/merge", body


def _create_issue(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    body: dict[str, Any] = {"title": a["title"]}
    for k in ("body", "labels", "assignees"):
        if a.get(k):
            body[k] = a[k]
    return "POST", f"/repos/{_repo(a)}/issues", body


def _comment(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return (
        "POST",
        f"/repos/{_repo(a)}/issues/{_seg(a['number'])}/comments",
        {"body": a["body"]},
    )


def _get_checks(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    return "GET", f"/repos/{_repo(a)}/commits/{_seg(a['ref'])}/check-runs", None


def _search_code(a: dict[str, Any]) -> "tuple[str, str, dict[str, Any] | None]":
    from urllib.parse import quote

    return "GET", f"/search/code?q={quote(str(a['q']))}", None


_ROUTES: "dict[str, _Builder]" = {
    "github.create_repo": _create_repo,
    "github.create_branch": _create_branch,
    "github.open_pr": _open_pr,
    "github.update_pr": _update_pr,
    "github.review_pr": _review_pr,
    "github.merge_pr": _merge_pr,
    "github.create_issue": _create_issue,
    "github.comment": _comment,
    "github.get_checks": _get_checks,
    "github.search_code": _search_code,
}
