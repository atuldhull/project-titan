"""Translate Jira webhooks into TITAN ``task.*`` events (Vol 7 §6.4). A human comment becomes
``task.comment_added`` (visible to the PM agent); a status change becomes
``task.remote_status_changed`` (the sync engine reconciles it via jira_sync.resolve_status).

The TITAN task id is read from the issue's ``titan-task-<id>`` label — a payload without one is
NOT a TITAN-managed issue and is ignored. Pure: no I/O ⇒ fixture-testable. Never raises on a
hostile shape."""

from typing import Any

from jira_api import TASK_LABEL_PREFIX


def _obj(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _task_id(issue: dict[str, Any]) -> str | None:
    labels = _obj(issue, "fields").get("labels")
    if isinstance(labels, list):
        for lbl in labels:
            if isinstance(lbl, str) and lbl.startswith(TASK_LABEL_PREFIX):
                return lbl[len(TASK_LABEL_PREFIX) :]
    return None


def _status_change(payload: dict[str, Any]) -> tuple[str, str] | None:
    for item in _obj(payload, "changelog").get("items") or []:
        if isinstance(item, dict) and item.get("field") == "status":
            return str(item.get("fromString", "")), str(item.get("toString", ""))
    return None


def translate(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    issue = _obj(payload, "issue")
    task_id = _task_id(issue)
    if not task_id:
        return []  # not a TITAN-managed issue
    key = issue.get("key")
    event = str(payload.get("webhookEvent", ""))

    # A comment (comment_created, or an update that carried a comment) → task.comment_added.
    comment = _obj(payload, "comment")
    if event == "comment_created" or comment:
        return [
            (
                "task.comment_added",
                {
                    "task_id": task_id,
                    "issue_key": key,
                    "author": _obj(comment, "author").get("displayName"),
                    "body": comment.get("body"),
                    "source": "jira",
                },
            )
        ]

    change = _status_change(payload)
    if change is not None:
        return [
            (
                "task.remote_status_changed",
                {
                    "task_id": task_id,
                    "issue_key": key,
                    "from_status": change[0],
                    "to_status": change[1],
                    "source": "jira",
                },
            )
        ]
    return []
