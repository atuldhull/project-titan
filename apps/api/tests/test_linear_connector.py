"""The first-party Linear sync connector (Phase 82; Vol 7 §6.4, TITAN-INT-043/011).

In-process against recorded fixtures (no network): the GraphQL tracker I/O tools (marker + auth +
GraphQL-error handling), Linear-webhook → task.* translation, and the sync_conflict resolver
(same algorithm as Jira). Skips cleanly if the connector directory is absent (INT-011)."""

import json
from typing import Any, cast

from tests.connector_loader import load_connector

ln_http: Any = load_connector("linear", "linear_http")
ln_api: Any = load_connector("linear", "linear_api")
ln_events: Any = load_connector("linear", "linear_events")
ln_sync: Any = load_connector("linear", "linear_sync")
ln_plugin: Any = load_connector("linear", "linear_plugin")


def _resp(status: int, body: dict[str, Any], **headers: str) -> Any:
    return ln_http.HttpResponse(status, dict(headers), json.dumps(body))


class _Caps:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


def _connector(transport: Any, caps: Any = None, config: dict[str, str] | None = None) -> Any:
    base = {"LINEAR_API_KEY": "lin_key", "LINEAR_TEAM_ID": "team-1"}
    return ln_plugin.LinearConnector(caps, http=transport, config={**base, **(config or {})})


def _body(tx: Any, i: int = 0) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(tx.calls[i][3]))


# --- GraphQL tracker I/O -----------------------------------------------------


def test_create_issue_embeds_marker_and_team() -> None:
    tx = ln_http.RecordedTransport().add(
        "POST",
        "/graphql",
        _resp(200, {"data": {"issueCreate": {"success": True, "issue": {"id": "i1"}}}}),
    )
    out = _connector(tx).call_tool(
        "linear.create_issue", {"task_id": "t1", "title": "Do it", "description": "why"}
    )
    assert out["ok"] and out["data"]["issueCreate"]["issue"]["id"] == "i1"
    b = _body(tx)
    assert "issueCreate" in b["query"]
    assert b["variables"]["input"]["teamId"] == "team-1"
    assert "[titan-task:t1]" in b["variables"]["input"]["description"]
    assert tx.calls[0][2]["authorization"] == "lin_key"  # API key verbatim (no Bearer)


def test_update_state_and_comment_and_find() -> None:
    tx = (
        ln_http.RecordedTransport()
        .add("POST", "/graphql", _resp(200, {"data": {"issueUpdate": {"success": True}}}))
        .add("POST", "/graphql", _resp(200, {"data": {"commentCreate": {"success": True}}}))
        .add("POST", "/graphql", _resp(200, {"data": {"issues": {"nodes": [{"id": "i1"}]}}}))
    )
    conn = _connector(tx)
    assert conn.call_tool("linear.update_issue", {"id": "i1", "state_id": "s-done"})["ok"]
    assert _body(tx, 0)["variables"]["input"]["stateId"] == "s-done"
    assert conn.call_tool("linear.add_comment", {"id": "i1", "body": "lgtm"})["ok"]
    assert _body(tx, 1)["variables"]["input"]["issueId"] == "i1"
    assert conn.call_tool("linear.find_by_task", {"task_id": "t1"})["ok"]
    assert _body(tx, 2)["variables"]["m"] == "titan-task:t1"


def test_oauth_bearer_and_graphql_error() -> None:
    tx = ln_http.RecordedTransport().add("POST", "/graphql", _resp(200, {}))
    _connector(tx, config={"LINEAR_OAUTH_TOKEN": "oauth"}).call_tool(
        "linear.get_issue", {"id": "i1"}
    )
    assert tx.calls[0][2]["authorization"] == "Bearer oauth"

    tx2 = ln_http.RecordedTransport().add(
        "POST", "/graphql", _resp(200, {"errors": [{"message": "Entity not found"}]})
    )
    out = _connector(tx2).call_tool("linear.get_issue", {"id": "bad"})
    assert out["error_code"] == "UPSTREAM_ERROR" and out["message"] == "Entity not found"


# --- inbound webhook translation ---------------------------------------------


def test_comment_and_status_webhooks_translate() -> None:
    caps = _Caps()
    comment = {
        "action": "create",
        "type": "Comment",
        "data": {
            "body": "clarify?",
            "user": {"name": "Dana"},
            "issue": {"id": "i1", "description": "spec\n\n[titan-task:t7]"},
        },
    }
    out = _connector(ln_http.RecordedTransport(), caps).on_webhook("/hooks/linear", comment)
    assert out["published"] == ["task.comment_added"]
    assert caps.published[0][1]["task_id"] == "t7" and caps.published[0][1]["source"] == "linear"

    status = {
        "action": "update",
        "type": "Issue",
        "data": {"id": "i1", "description": "[titan-task:t7]", "state": {"name": "Done"}},
        "updatedFrom": {"stateId": "s-old"},
    }
    ev = ln_events.translate(status)
    assert ev[0][0] == "task.remote_status_changed" and ev[0][1]["to_status"] == "Done"

    # an issue update that did NOT change state, and one with no marker → ignored
    assert (
        ln_events.translate(
            {
                "action": "update",
                "type": "Issue",
                "data": {"description": "[titan-task:t7]"},
                "updatedFrom": {"title": "x"},
            }
        )
        == []
    )
    assert (
        ln_events.translate(
            {
                "action": "update",
                "type": "Issue",
                "data": {"description": "no marker"},
                "updatedFrom": {"stateId": "s"},
            }
        )
        == []
    )


# --- INT-043 sync algorithm (duplicated module) ------------------------------

_MAP = {"todo": "s-todo", "in_progress": "s-prog", "done": "s-done"}


def test_conflict_and_mirror() -> None:
    conflict = ln_sync.resolve_status(
        titan_status="done",
        tracker_status="s-prog",
        last_synced_titan="todo",
        last_synced_tracker="s-todo",
        state_map=_MAP,
    )
    assert conflict.action == "conflict"
    mirror = ln_sync.resolve_status(
        titan_status="in_progress",
        tracker_status="s-todo",
        last_synced_titan="todo",
        last_synced_tracker="s-todo",
        state_map=_MAP,
    )
    assert mirror.action == "apply_to_tracker" and mirror.tracker_status == "s-prog"


def test_unknown_tool_and_bad_arg() -> None:
    conn = _connector(ln_http.RecordedTransport())
    assert conn.call_tool("linear.nope", {})["error_code"] == "UNKNOWN_TOOL"
    assert conn.call_tool("linear.get_issue", {})["error_code"] == "BAD_ARGUMENT"
