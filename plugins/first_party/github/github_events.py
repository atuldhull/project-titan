"""Translate GitHub webhook payloads into TITAN ``vcs.*`` events (Vol 7 §6.1) that drive
the Volume-6 review/deploy workflow on Redis Streams.

The core webhook route verifies ``X-Hub-Signature-256`` and hands the plugin the PARSED
body only — NOT the ``X-GitHub-Event`` header — so the event kind is inferred from the
payload's own discriminating keys (push has ``ref``+``commits`` and no ``action``;
pull_request/issue_comment/check_suite carry ``action`` plus their object). Pure: no I/O,
so it is exhaustively fixture-testable. Returns ``[(topic, payload), …]`` (possibly empty
for an event we do not mirror)."""

from typing import Any

#: Every topic this translator can emit — the manifest's events.publish grant must cover
#: exactly this set (an ungranted topic is denied by the broker).
TOPICS = (
    "vcs.pr.opened",
    "vcs.pr.updated",
    "vcs.pr.closed",
    "vcs.pr.merged",
    "vcs.pr.review_requested",
    "vcs.pr.commented",
    "vcs.checks.completed",
    "vcs.push",
)


def translate(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    commits = payload.get("commits")
    if isinstance(commits, list) and "ref" in payload and "action" not in payload:
        return _push(payload)
    action = str(payload.get("action", ""))
    # A `pull_request` event carries a top-level `pull_request` and NEITHER a top-level
    # `review` (that is pull_request_review) NOR a top-level `comment` (that is
    # pull_request_review_comment) — those share the pull_request key but must NOT be mapped
    # to vcs.pr.updated (they would spuriously re-trigger the review/deploy workflow).
    if (
        "pull_request" in payload
        and "review" not in payload
        and "comment" not in payload
    ):
        return _pull_request(action, payload)
    if "check_suite" in payload:
        return _check_suite(action, payload)
    if "comment" in payload and "issue" in payload:
        return _issue_comment(action, payload)
    return []


def _obj(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """A nested object field as a dict — a hostile webhook could send a list/scalar; never
    let that raise out of translation (three faults would auto-quarantine the connector)."""
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _repo_full(payload: dict[str, Any]) -> str:
    return str(_obj(payload, "repository").get("full_name", ""))


def _push(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "vcs.push",
            {
                "repo": _repo_full(payload),
                "ref": payload.get("ref"),
                "before": payload.get("before"),
                "after": payload.get("after"),
                "commits": len(payload["commits"]),  # translate() proved this is a list
                "pusher": _obj(payload, "pusher").get("name"),
            },
        )
    ]


def _pull_request(
    action: str, payload: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    pr = _obj(payload, "pull_request")
    base = {
        "repo": _repo_full(payload),
        "number": payload.get("number") or pr.get("number"),
        "title": pr.get("title"),
        "head": _obj(pr, "head").get("ref"),
        "base": _obj(pr, "base").get("ref"),
        "url": pr.get("html_url"),
        "action": action,
    }
    if action == "review_requested":
        base["requested_reviewer"] = _obj(payload, "requested_reviewer").get("login")
        return [("vcs.pr.review_requested", base)]
    if action == "opened":
        return [("vcs.pr.opened", base)]
    if action == "closed":
        merged = bool(pr.get("merged"))
        return [
            ("vcs.pr.merged" if merged else "vcs.pr.closed", {**base, "merged": merged})
        ]
    if action in ("synchronize", "edited", "ready_for_review", "reopened"):
        return [("vcs.pr.updated", base)]
    return []


def _check_suite(
    action: str, payload: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    if action != "completed":
        return []
    suite = _obj(payload, "check_suite")
    return [
        (
            "vcs.checks.completed",
            {
                "repo": _repo_full(payload),
                "head_sha": suite.get("head_sha"),
                "head_branch": suite.get("head_branch"),
                "conclusion": suite.get("conclusion"),
                "status": suite.get("status"),
            },
        )
    ]


def _issue_comment(
    action: str, payload: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    if action != "created":
        return []
    issue = _obj(payload, "issue")
    comment = _obj(payload, "comment")
    return [
        (
            "vcs.pr.commented",
            {
                "repo": _repo_full(payload),
                "number": issue.get("number"),
                "is_pull_request": "pull_request" in issue,
                "author": _obj(comment, "user").get("login"),
                "body": comment.get("body"),
                "url": comment.get("html_url"),
            },
        )
    ]
