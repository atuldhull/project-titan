"""The first-party GitLab connector (Phase 82; Vol 7 §6.2, TITAN-INT-041/011).

In-process against recorded fixtures (no network): the merge-request lifecycle driven THROUGH
the generic vcs.* aliases (the INT-041 acceptance — a provider-neutral workflow runs with GitLab
as the only VCS connector), provider-specific tools, and GitLab-webhook → vcs.* translation.
Skips cleanly if the connector directory is absent (INT-011)."""

import json
from typing import Any

from tests.connector_loader import load_connector

gl_http: Any = load_connector("gitlab", "gitlab_http")
gl_api: Any = load_connector("gitlab", "gitlab_api")
gl_events: Any = load_connector("gitlab", "gitlab_events")
gl_plugin: Any = load_connector("gitlab", "gitlab_plugin")


def _resp(status: int, body: dict[str, Any] | list[Any], **headers: str) -> Any:
    return gl_http.HttpResponse(status, dict(headers), json.dumps(body))


class _Caps:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


def _connector(transport: Any, caps: Any = None) -> Any:
    return gl_plugin.GitlabConnector(
        caps,
        http=transport,
        config={"GITLAB_TOKEN": "glpat-x", "GITLAB_API_URL": "https://gitlab.com/api/v4"},
    )


# --- the MR lifecycle THROUGH the vcs.* aliases (INT-041) ---------------------


def test_mr_lifecycle_via_vcs_aliases() -> None:
    tx = (
        gl_http.RecordedTransport()
        .add("POST", "/repository/branches", _resp(201, {"name": "feature"}))
        .add("POST", "/merge_requests", _resp(201, {"iid": 7, "web_url": "u"}))
        .add("POST", "/merge_requests/7/approve", _resp(201, {"state": "approved"}))
        .add("PUT", "/merge_requests/7/merge", _resp(200, {"state": "merged"}))
    )
    conn = _connector(tx)
    proj = {"project": "grp/app"}
    assert conn.call_tool("vcs.create_branch", {**proj, "branch": "feature", "ref": "main"})["ok"]
    assert conn.call_tool(
        "vcs.open_change_request",
        {**proj, "source_branch": "feature", "target_branch": "main", "title": "T"},
    )["ok"]
    assert conn.call_tool("vcs.review", {**proj, "iid": 7, "event": "approve"})["ok"]
    assert conn.call_tool("vcs.merge", {**proj, "iid": 7})["ok"]

    # every call hit the right project (url-encoded path) with the PAT header
    urls = [c[1] for c in tx.calls]
    assert any(
        "/projects/grp%2Fapp/repository/branches?" in u and "branch=feature" in u for u in urls
    )
    assert any(u.endswith("/projects/grp%2Fapp/merge_requests") for u in urls)
    assert any(u.endswith("/merge_requests/7/approve") for u in urls)
    assert any(u.endswith("/merge_requests/7/merge") for u in urls)
    assert all(c[2]["private-token"] == "glpat-x" for c in tx.calls)


def test_vcs_review_without_approve_posts_a_note() -> None:
    tx = gl_http.RecordedTransport().add("POST", "/merge_requests/7/notes", _resp(201, {"id": 1}))
    out = _connector(tx).call_tool("vcs.review", {"project": "g/a", "iid": 7, "body": "please fix"})
    assert out["ok"] and tx.calls[0][1].endswith("/merge_requests/7/notes")
    assert json.loads(tx.calls[0][3]) == {"body": "please fix"}


def test_provider_specific_tools_and_pipeline_read() -> None:
    tx = (
        gl_http.RecordedTransport()
        .add("POST", "/issues", _resp(201, {"iid": 3}))
        .add("GET", "/repository/commits/abc/statuses", _resp(200, [{"status": "success"}]))
    )
    conn = _connector(tx)
    assert conn.call_tool(
        "gitlab.create_issue", {"project": "g/a", "title": "Bug", "labels": ["p1", "ux"]}
    )["ok"]
    assert json.loads(tx.calls[0][3])["labels"] == "p1,ux"
    assert conn.call_tool("gitlab.get_pipelines", {"project": "g/a", "sha": "abc"})["ok"]
    assert "/repository/commits/abc/statuses" in tx.calls[1][1]


# --- webhook → vcs.* translation ---------------------------------------------


def test_merge_request_webhook_translates_to_vcs_opened() -> None:
    caps = _Caps()
    payload = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "grp/app"},
        "object_attributes": {
            "iid": 7,
            "title": "Add feature",
            "action": "open",
            "source_branch": "feature",
            "target_branch": "main",
            "url": "u",
        },
    }
    out = _connector(gl_http.RecordedTransport(), caps).on_webhook("/hooks/gitlab", payload)
    assert out["published"] == ["vcs.pr.opened"]
    topic, ev = caps.published[0]
    assert topic == "vcs.pr.opened" and ev["repo"] == "grp/app" and ev["number"] == 7
    assert ev["head"] == "feature" and ev["base"] == "main"


def test_merge_action_maps_to_merged() -> None:
    p = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "g/a"},
        "object_attributes": {"iid": 1, "action": "merge"},
    }
    assert gl_events.translate(p) == [
        (
            "vcs.pr.merged",
            {
                "repo": "g/a",
                "number": 1,
                "title": None,
                "head": None,
                "base": None,
                "url": None,
                "action": "merge",
                "merged": True,
            },
        )
    ]


def test_pipeline_and_push_and_note_translation() -> None:
    pipe = {
        "object_kind": "pipeline",
        "project": {"path_with_namespace": "g/a"},
        "object_attributes": {"status": "failed", "sha": "deadbeef", "ref": "main"},
    }
    assert gl_events.translate(pipe)[0][0] == "vcs.checks.completed"
    assert gl_events.translate(pipe)[0][1]["conclusion"] == "failed"

    push = {
        "object_kind": "push",
        "project": {"path_with_namespace": "g/a"},
        "ref": "refs/heads/main",
        "commits": [{}, {}],
        "user_username": "dev",
    }
    assert gl_events.translate(push) == [
        (
            "vcs.push",
            {
                "repo": "g/a",
                "ref": "refs/heads/main",
                "before": None,
                "after": None,
                "commits": 2,
                "pusher": "dev",
            },
        )
    ]

    note = {
        "object_kind": "note",
        "project": {"path_with_namespace": "g/a"},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "lgtm", "url": "u"},
        "merge_request": {"iid": 9},
        "user": {"username": "rev"},
    }
    assert gl_events.translate(note)[0][0] == "vcs.pr.commented"
    assert gl_events.translate(note)[0][1]["number"] == 9

    # a pipeline still running, and an issue note, are NOT mirrored
    assert (
        gl_events.translate({"object_kind": "pipeline", "object_attributes": {"status": "running"}})
        == []
    )
    assert (
        gl_events.translate(
            {"object_kind": "note", "object_attributes": {"noteable_type": "Issue"}}
        )
        == []
    )


# --- soft-error envelope + unknown tool --------------------------------------


def test_soft_errors_and_unknown_tool() -> None:
    conn = _connector(
        gl_http.RecordedTransport().add("PUT", "/merge", _resp(429, {}, **{"retry-after": "9"}))
    )
    out = conn.call_tool("vcs.merge", {"project": "g/a", "iid": 1})
    assert out["error_code"] == "RATE_LIMITED" and out["retry_after"] == 9

    conn2 = _connector(
        gl_http.RecordedTransport().add(
            "POST", "/merge_requests", _resp(409, {"message": "conflict"})
        )
    )
    out2 = conn2.call_tool(
        "gitlab.open_mr",
        {"project": "g/a", "source_branch": "a", "target_branch": "b", "title": "t"},
    )
    assert out2["error_code"] == "UPSTREAM_ERROR" and out2["status"] == 409

    assert (
        _connector(gl_http.RecordedTransport()).call_tool("gitlab.nope", {})["error_code"]
        == "UNKNOWN_TOOL"
    )
    assert (
        _connector(gl_http.RecordedTransport()).call_tool("vcs.merge", {})["error_code"]
        == "BAD_ARGUMENT"
    )
