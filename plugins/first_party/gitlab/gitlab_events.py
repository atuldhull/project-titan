"""Translate GitLab webhook payloads into TITAN ``vcs.*`` events (Vol 7 §6.2) — the SAME topic
set the GitHub connector emits, so the Volume-6 review/deploy workflow is provider-neutral.

GitLab gives a clean ``object_kind`` discriminator in the body (unlike GitHub's inference), so
translation is a simple dispatch. Pure: no I/O ⇒ exhaustively fixture-testable. Returns
``[(topic, payload), …]`` (empty for an event we do not mirror). Never raises on a hostile shape
(three faults would auto-quarantine the connector)."""

from typing import Any

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

_FINISHED = {"success", "failed", "canceled", "skipped"}


def _obj(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _repo(payload: dict[str, Any]) -> str:
    return str(_obj(payload, "project").get("path_with_namespace", ""))


def translate(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    kind = str(payload.get("object_kind", ""))
    if kind == "push":
        return _push(payload)
    if kind == "merge_request":
        return _merge_request(payload)
    if kind == "pipeline":
        return _pipeline(payload)
    if kind == "note":
        return _note(payload)
    return []


def _push(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    commits = payload.get("commits")
    return [
        (
            "vcs.push",
            {
                "repo": _repo(payload),
                "ref": payload.get("ref"),
                "before": payload.get("before"),
                "after": payload.get("after"),
                "commits": len(commits) if isinstance(commits, list) else 0,
                "pusher": payload.get("user_username"),
            },
        )
    ]


def _merge_request(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    oa = _obj(payload, "object_attributes")
    action = str(oa.get("action", ""))
    base = {
        "repo": _repo(payload),
        "number": oa.get("iid"),
        "title": oa.get("title"),
        "head": oa.get("source_branch"),
        "base": oa.get("target_branch"),
        "url": oa.get("url"),
        "action": action,
    }
    if action in ("open", "reopen"):
        return [("vcs.pr.opened", base)]
    if action == "merge":
        return [("vcs.pr.merged", {**base, "merged": True})]
    if action == "close":
        return [("vcs.pr.closed", {**base, "merged": False})]
    if action in ("update", "approved", "unapproved"):
        return [("vcs.pr.updated", base)]
    return []


def _pipeline(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    oa = _obj(payload, "object_attributes")
    status = str(oa.get("status", ""))
    if status not in _FINISHED:
        return []
    return [
        (
            "vcs.checks.completed",
            {
                "repo": _repo(payload),
                "head_sha": oa.get("sha"),
                "head_branch": oa.get("ref"),
                "conclusion": status,
                "status": "completed",
            },
        )
    ]


def _note(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    oa = _obj(payload, "object_attributes")
    if str(oa.get("noteable_type", "")) != "MergeRequest":
        return []  # only MR notes drive the review workflow
    mr = _obj(payload, "merge_request")
    return [
        (
            "vcs.pr.commented",
            {
                "repo": _repo(payload),
                "number": mr.get("iid"),
                "is_pull_request": True,
                "author": _obj(payload, "user").get("username"),
                "body": oa.get("note"),
                "url": oa.get("url"),
            },
        )
    ]
