"""The first-party Jira sync connector (Phase 82; Vol 7 §6.4, TITAN-INT-043/011).

In-process against recorded fixtures (no network): the tracker I/O tools (with the titan-task
label + ADF bodies + auth), jira:issue_updated → task.* translation, and — the INT-043 heart —
the state map + the sync_conflict RESOLVER (a seeded simultaneous edit is flagged, not
overwritten). Skips cleanly if the connector directory is absent (INT-011)."""

import json
from typing import Any

from tests.connector_loader import load_connector

jr_http: Any = load_connector("jira", "jira_http")
jr_api: Any = load_connector("jira", "jira_api")
jr_events: Any = load_connector("jira", "jira_events")
jr_sync: Any = load_connector("jira", "jira_sync")
jr_plugin: Any = load_connector("jira", "jira_plugin")


def _resp(status: int, body: dict[str, Any], **headers: str) -> Any:
    return jr_http.HttpResponse(status, dict(headers), json.dumps(body))


class _Caps:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


def _connector(transport: Any, caps: Any = None, config: dict[str, str] | None = None) -> Any:
    base = {
        "JIRA_BASE_URL": "https://acme.atlassian.net",
        "JIRA_EMAIL": "bot@acme.co",
        "JIRA_API_TOKEN": "tok",
    }
    return jr_plugin.JiraConnector(caps, http=transport, config={**base, **(config or {})})


# --- tracker I/O -------------------------------------------------------------


def test_create_issue_stamps_titan_label_and_adf() -> None:
    tx = jr_http.RecordedTransport().add("POST", "/rest/api/3/issue", _resp(201, {"key": "ACME-1"}))
    out = _connector(tx).call_tool(
        "jira.create_issue",
        {"project_key": "ACME", "task_id": "abc-123", "summary": "Do it", "description": "why"},
    )
    assert out["ok"] and out["data"]["key"] == "ACME-1"
    fields = json.loads(tx.calls[0][3])["fields"]
    assert fields["labels"] == ["titan-task-abc-123"]
    assert fields["description"]["type"] == "doc"  # ADF-wrapped
    assert tx.calls[0][2]["authorization"].startswith("Basic ")


def test_transition_and_comment_and_find() -> None:
    tx = (
        jr_http.RecordedTransport()
        .add("POST", "/ACME-1/transitions", _resp(204, {}))
        .add("POST", "/ACME-1/comment", _resp(201, {"id": "9"}))
        .add("GET", "/rest/api/3/search", _resp(200, {"issues": [{"key": "ACME-1"}]}))
    )
    conn = _connector(tx)
    assert conn.call_tool("jira.transition_issue", {"key": "ACME-1", "transition_id": 31})["ok"]
    assert json.loads(tx.calls[0][3]) == {"transition": {"id": "31"}}
    assert conn.call_tool("jira.add_comment", {"key": "ACME-1", "body": "lgtm"})["ok"]
    assert json.loads(tx.calls[1][3])["body"]["type"] == "doc"
    assert conn.call_tool("jira.find_by_task", {"task_id": "abc-123"})["ok"]
    assert "titan-task-abc-123" in tx.calls[2][1]


def test_oauth_bearer_preferred() -> None:
    tx = jr_http.RecordedTransport().add("GET", "/issue/ACME-1", _resp(200, {}))
    _connector(tx, config={"JIRA_OAUTH_TOKEN": "oauth"}).call_tool(
        "jira.get_issue", {"key": "ACME-1"}
    )
    assert tx.calls[0][2]["authorization"] == "Bearer oauth"


# --- inbound webhook translation ---------------------------------------------


def _issue(labels: list[str], **fields: Any) -> dict[str, Any]:
    return {"key": "ACME-1", "fields": {"labels": labels, **fields}}


def test_comment_webhook_becomes_task_comment_added() -> None:
    caps = _Caps()
    payload = {
        "webhookEvent": "comment_created",
        "issue": _issue(["titan-task-abc-123"]),
        "comment": {"body": "please clarify", "author": {"displayName": "Dana"}},
    }
    out = _connector(jr_http.RecordedTransport(), caps).on_webhook("/hooks/jira", payload)
    assert out["published"] == ["task.comment_added"]
    topic, ev = caps.published[0]
    assert ev["task_id"] == "abc-123" and ev["author"] == "Dana" and ev["source"] == "jira"


def test_status_change_webhook_and_unmanaged_issue_ignored() -> None:
    status_change = {
        "webhookEvent": "jira:issue_updated",
        "issue": _issue(["titan-task-t9"]),
        "changelog": {"items": [{"field": "status", "fromString": "To Do", "toString": "Done"}]},
    }
    ev = jr_events.translate(status_change)
    assert ev[0][0] == "task.remote_status_changed"
    assert ev[0][1]["task_id"] == "t9" and ev[0][1]["to_status"] == "Done"

    # an issue with NO titan-task label is not TITAN-managed → ignored
    assert (
        jr_events.translate({"webhookEvent": "jira:issue_updated", "issue": _issue(["other"])})
        == []
    )


# --- INT-043: the sync algorithm (state map + conflict) ----------------------

_MAP = {"todo": "To Do", "in_progress": "In Progress", "review": "In Review", "done": "Done"}


def test_state_map_both_directions() -> None:
    assert jr_sync.titan_to_tracker("in_progress", _MAP) == "In Progress"
    assert jr_sync.tracker_to_titan("in review", _MAP) == "review"  # case-insensitive
    assert jr_sync.tracker_to_titan("Unknown", _MAP) is None


def test_only_titan_moved_mirrors_to_tracker() -> None:
    d = jr_sync.resolve_status(
        titan_status="in_progress",
        tracker_status="To Do",
        last_synced_titan="todo",
        last_synced_tracker="To Do",
        state_map=_MAP,
    )
    assert d.action == "apply_to_tracker" and d.tracker_status == "In Progress"


def test_only_tracker_moved_applies_to_titan() -> None:
    d = jr_sync.resolve_status(
        titan_status="todo",
        tracker_status="Done",
        last_synced_titan="todo",
        last_synced_tracker="To Do",
        state_map=_MAP,
    )
    assert d.action == "apply_to_titan" and d.titan_status == "done"


def test_parity_is_a_noop() -> None:
    d = jr_sync.resolve_status(
        titan_status="done",
        tracker_status="Done",
        last_synced_titan="todo",
        last_synced_tracker="To Do",
        state_map=_MAP,
    )
    assert d.action == "in_parity"


def test_simultaneous_conflicting_transitions_flag_sync_conflict() -> None:
    # both sides moved from the To Do/todo baseline, to DIFFERENT states → conflict (INT-043 DoD)
    d = jr_sync.resolve_status(
        titan_status="done",
        tracker_status="In Progress",
        last_synced_titan="todo",
        last_synced_tracker="To Do",
        state_map=_MAP,
    )
    assert d.action == "conflict"
    assert d.titan_status == "done" and d.tracker_status == "In Progress"


def test_soft_errors_and_unknown_tool() -> None:
    tx = jr_http.RecordedTransport().add(
        "POST", "/rest/api/3/issue", _resp(429, {}, **{"retry-after": "5"})
    )
    out = _connector(tx).call_tool(
        "jira.create_issue", {"project_key": "A", "task_id": "x", "summary": "s"}
    )
    assert out["error_code"] == "RATE_LIMITED" and out["retry_after"] == 5
    assert (
        _connector(jr_http.RecordedTransport()).call_tool("jira.nope", {})["error_code"]
        == "UNKNOWN_TOOL"
    )
    assert (
        _connector(jr_http.RecordedTransport()).call_tool("jira.get_issue", {})["error_code"]
        == "BAD_ARGUMENT"
    )
