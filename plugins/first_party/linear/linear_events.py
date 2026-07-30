"""Translate Linear webhooks into TITAN ``task.*`` events (Vol 7 §6.4). A human comment →
``task.comment_added``; an issue state change → ``task.remote_status_changed``. The TITAN task id
is parsed from the issue description's ``titan-task:<id>`` marker; a payload without one is not a
TITAN-managed issue and is ignored. Pure: no I/O ⇒ fixture-testable. Never raises on a hostile
shape.

Linear webhook shape: ``{action, type, data, updatedFrom}`` — type Comment/Issue, action
create/update; ``updatedFrom`` carries the previous values of changed fields.
"""

import re
from typing import Any

_MARKER = re.compile(r"titan-task:([^\]\s]+)")


def _obj(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _task_id_from_description(description: Any) -> str | None:
    if not isinstance(description, str):
        return None
    m = _MARKER.search(description)
    return m.group(1) if m else None


def translate(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    action = str(payload.get("action", ""))
    kind = str(payload.get("type", ""))
    data = _obj(payload, "data")

    if kind == "Comment" and action == "create":
        issue = _obj(data, "issue")
        task_id = _task_id_from_description(issue.get("description"))
        if not task_id:
            return []
        return [
            (
                "task.comment_added",
                {
                    "task_id": task_id,
                    "issue_id": issue.get("id"),
                    "author": _obj(data, "user").get("name"),
                    "body": data.get("body"),
                    "source": "linear",
                },
            )
        ]

    if kind == "Issue" and action == "update":
        task_id = _task_id_from_description(data.get("description"))
        if not task_id:
            return []
        # A state transition: `updatedFrom` names the changed fields (stateId present ⇒ moved).
        if "stateId" not in (payload.get("updatedFrom") or {}):
            return []
        return [
            (
                "task.remote_status_changed",
                {
                    "task_id": task_id,
                    "issue_id": data.get("id"),
                    "to_status": _obj(data, "state").get("name"),
                    "source": "linear",
                },
            )
        ]
    return []
