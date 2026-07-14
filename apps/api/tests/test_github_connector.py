"""The first-party GitHub connector (Phase 54; Vol 7 §6.1, TITAN-INT-040/011).

In-process against recorded fixtures (no network): the full PR lifecycle, the rate-limit
token bucket, App-JWT construction, and webhook → vcs.* translation. The webhook path
THROUGH the real core route (signature verify + event publish) is in test_connectors_api.py.
Skips cleanly if the connector directory is absent (INT-011)."""

import json
from typing import Any

from tests.connector_loader import load_connector

gh_http: Any = load_connector("github", "github_http")
gh_api: Any = load_connector("github", "github_api")
gh_auth: Any = load_connector("github", "github_auth")
gh_events: Any = load_connector("github", "github_events")
gh_plugin: Any = load_connector("github", "github_plugin")


def _resp(status: int, body: dict[str, Any], **headers: str) -> Any:
    return gh_http.HttpResponse(status, dict(headers), json.dumps(body))


def _fresh_headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": "9999999999",
    }


class _Caps:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self.published: list[tuple[str, dict[str, Any]]] = []

    def secret(self, name: str) -> str:
        if name not in self._secrets:
            raise RuntimeError("not granted")
        return self._secrets[name]

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


def _connector(transport: Any, caps: Any = None) -> Any:
    return gh_plugin.GithubConnector(
        caps,
        http=transport,
        config={"GITHUB_TOKEN": "ghp_x", "GITHUB_API_URL": "https://api.github.com"},
    )


# --- the full PR lifecycle (recorded fixtures, TITAN-INT-040) ----------------


def test_full_pr_lifecycle_branch_pr_review_checks_merge() -> None:
    tx = gh_http.RecordedTransport()
    h = _fresh_headers()
    tx.add(
        "POST", "/git/refs", gh_http.HttpResponse(201, h, json.dumps({"ref": "refs/heads/feature"}))
    )
    tx.add(
        "POST", "/pulls", gh_http.HttpResponse(201, h, json.dumps({"number": 42, "html_url": "u"}))
    )
    tx.add(
        "POST", "/pulls/42/reviews", gh_http.HttpResponse(200, h, json.dumps({"state": "APPROVED"}))
    )
    tx.add(
        "GET",
        "/check-runs",
        gh_http.HttpResponse(
            200, h, json.dumps({"total_count": 1, "check_runs": [{"conclusion": "success"}]})
        ),
    )
    tx.add(
        "PUT",
        "/pulls/42/merge",
        gh_http.HttpResponse(200, h, json.dumps({"merged": True, "sha": "abc"})),
    )
    conn = _connector(tx)

    branch = conn.call_tool(
        "github.create_branch", {"owner": "o", "repo": "r", "branch": "feature", "sha": "base1"}
    )
    pr = conn.call_tool(
        "github.open_pr",
        {"owner": "o", "repo": "r", "title": "T", "head": "feature", "base": "main"},
    )
    review = conn.call_tool(
        "github.review_pr", {"owner": "o", "repo": "r", "number": 42, "event": "APPROVE"}
    )
    checks = conn.call_tool("github.get_checks", {"owner": "o", "repo": "r", "ref": "abc"})
    merge = conn.call_tool("github.merge_pr", {"owner": "o", "repo": "r", "number": 42})

    assert branch["ok"] and pr["data"]["number"] == 42
    assert review["ok"] and review["data"]["state"] == "APPROVED"
    assert checks["ok"] and checks["data"]["check_runs"][0]["conclusion"] == "success"
    assert merge["ok"] and merge["data"]["merged"] is True
    # Every request carried the bearer token + the pinned API version.
    for _method, _url, headers, _body in tx.calls:
        assert headers["authorization"] == "Bearer ghp_x"
        assert headers["x-github-api-version"] == gh_api.API_VERSION


def test_api_error_surfaces_as_upstream_error_not_a_crash() -> None:
    tx = gh_http.RecordedTransport()
    tx.add(
        "POST",
        "/pulls",
        gh_http.HttpResponse(422, _fresh_headers(), json.dumps({"message": "Validation Failed"})),
    )
    out = _connector(tx).call_tool(
        "github.open_pr", {"owner": "o", "repo": "r", "title": "T", "head": "h", "base": "b"}
    )
    assert out["ok"] is False and out["error_code"] == "UPSTREAM_ERROR"
    assert out["status"] == 422 and "Validation" in out["message"]


# --- rate limiting (TITAN-INT-040) -------------------------------------------


def test_write_is_rate_limited_below_ten_percent_with_retry_after() -> None:
    rl: Any = load_connector("github", "github_ratelimit")
    tx = gh_http.RecordedTransport()
    bucket = rl.RateBucket(limit=5000, remaining=100, reset_epoch=2_000_000_000)
    client = gh_api.GithubClient(
        tx,
        gh_auth.GithubAuth({"GITHUB_TOKEN": "x"}, tx, api_url="https://api.github.com"),
        api_url="https://api.github.com",
        bucket=bucket,
        clock=lambda: 1_000_000_000,
    )
    out = client.call("github.merge_pr", {"owner": "o", "repo": "r", "number": 1})
    assert out["ok"] is False and out["error_code"] == "RATE_LIMITED"
    assert out["retry_after"] == 1_000_000_000  # reset - now
    assert tx.calls == []  # refused BEFORE any request was sent


def test_reads_are_not_pre_throttled() -> None:
    rl = load_connector("github", "github_ratelimit")
    tx = gh_http.RecordedTransport()
    tx.add(
        "GET",
        "/check-runs",
        gh_http.HttpResponse(200, _fresh_headers(), json.dumps({"check_runs": []})),
    )
    bucket = rl.RateBucket(limit=5000, remaining=1, reset_epoch=2_000_000_000)
    assert bucket.should_throttle()
    client = gh_api.GithubClient(
        tx,
        gh_auth.GithubAuth({"GITHUB_TOKEN": "x"}, tx, api_url="https://api.github.com"),
        api_url="https://api.github.com",
        bucket=bucket,
    )
    out = client.call("github.get_checks", {"owner": "o", "repo": "r", "ref": "sha"})
    assert out["ok"] is True  # a read still goes through when the budget is low


# --- App auth (JWT construction with an injected signer) ----------------------


def test_app_installation_token_exchange_uses_a_signed_jwt() -> None:
    signed: dict[str, bytes] = {}

    def fake_signer(signing_input: bytes, private_key_pem: str) -> bytes:
        signed["input"] = signing_input
        signed["key"] = private_key_pem.encode()
        return b"fake-signature"

    tx = gh_http.RecordedTransport()
    tx.add(
        "POST",
        "/access_tokens",
        gh_http.HttpResponse(
            201, {}, json.dumps({"token": "ghs_inst", "expires_at": "2999-01-01T00:00:00Z"})
        ),
    )
    auth = gh_auth.GithubAuth(
        {
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": "PEM",
        },
        tx,
        api_url="https://api.github.com",
        signer=fake_signer,
        clock=lambda: 1000,
    )
    assert auth.is_app
    assert auth.token() == "ghs_inst"
    # The exchange used a Bearer JWT (three dot-separated parts) built from our claims.
    exchange_headers = tx.calls[-1][2]
    jwt = exchange_headers["authorization"].removeprefix("Bearer ")
    assert jwt.count(".") == 2 and jwt.endswith(gh_auth._b64url(b"fake-signature"))
    header, claims = jwt.split(".")[:2]
    import base64

    payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert payload["iss"] == "123" and payload["exp"] - payload["iat"] == 660
    # Cached: a second call does not re-exchange.
    assert auth.token() == "ghs_inst" and len([c for c in tx.calls if "access_tokens" in c[1]]) == 1


# --- webhook → vcs.* translation (TITAN-INT-040) -----------------------------


def test_webhook_translation_covers_the_four_event_kinds() -> None:
    caps = _Caps({})
    conn = gh_plugin.GithubConnector(caps, config={"GITHUB_TOKEN": "x"})

    pr = conn.on_webhook(
        "/hooks/github",
        {
            "action": "review_requested",
            "number": 3,
            "pull_request": {"title": "T", "head": {"ref": "f"}, "base": {"ref": "main"}},
            "requested_reviewer": {"login": "bob"},
            "repository": {"full_name": "o/r"},
        },
    )
    push = conn.on_webhook(
        "/hooks/github",
        {
            "ref": "refs/heads/main",
            "before": "a",
            "after": "b",
            "commits": [{}, {}],
            "pusher": {"name": "me"},
            "repository": {"full_name": "o/r"},
        },
    )
    checks = conn.on_webhook(
        "/hooks/github",
        {
            "action": "completed",
            "check_suite": {"head_sha": "s", "conclusion": "failure", "status": "completed"},
            "repository": {"full_name": "o/r"},
        },
    )
    comment = conn.on_webhook(
        "/hooks/github",
        {
            "action": "created",
            "issue": {"number": 3, "pull_request": {}},
            "comment": {"body": "lgtm", "user": {"login": "rev"}},
            "repository": {"full_name": "o/r"},
        },
    )

    assert pr["published"] == ["vcs.pr.review_requested"]
    assert push["published"] == ["vcs.push"]
    assert checks["published"] == ["vcs.checks.completed"]
    assert comment["published"] == ["vcs.pr.commented"]
    topics = [t for t, _ in caps.published]
    assert topics == [
        "vcs.pr.review_requested",
        "vcs.push",
        "vcs.checks.completed",
        "vcs.pr.commented",
    ]
    # Every emitted topic is one the manifest grants (events.publish).
    assert set(topics) <= set(gh_events.TOPICS)
    # The push payload carried the translated fields.
    assert caps.published[1][1]["commits"] == 2 and caps.published[1][1]["pusher"] == "me"


def test_pr_closed_distinguishes_merged_from_closed() -> None:
    conn = gh_plugin.GithubConnector(_Caps({}), config={"GITHUB_TOKEN": "x"})
    merged = conn.on_webhook(
        "/hooks/github",
        {
            "action": "closed",
            "number": 1,
            "pull_request": {"merged": True, "head": {"ref": "f"}, "base": {"ref": "m"}},
            "repository": {"full_name": "o/r"},
        },
    )
    closed = conn.on_webhook(
        "/hooks/github",
        {
            "action": "closed",
            "number": 2,
            "pull_request": {"merged": False, "head": {"ref": "f"}, "base": {"ref": "m"}},
            "repository": {"full_name": "o/r"},
        },
    )
    assert merged["published"] == ["vcs.pr.merged"]
    assert closed["published"] == ["vcs.pr.closed"]


def test_review_and_review_comment_events_are_not_misread_as_pr_updated() -> None:
    """A pull_request_review / pull_request_review_comment carries a top-level pull_request
    but must NOT emit vcs.pr.updated (that would spuriously re-trigger the workflow)."""
    conn = gh_plugin.GithubConnector(_Caps({}), config={"GITHUB_TOKEN": "x"})
    review = conn.on_webhook(
        "/hooks/github",
        {
            "action": "edited",
            "review": {"state": "x"},
            "pull_request": {"number": 1},
            "repository": {"full_name": "o/r"},
        },
    )
    review_comment = conn.on_webhook(
        "/hooks/github",
        {
            "action": "edited",
            "comment": {"body": "x"},
            "pull_request": {"number": 1},
            "repository": {"full_name": "o/r"},
        },
    )
    assert review["published"] == [] and review_comment["published"] == []


def test_translate_is_hardened_against_hostile_payload_shapes() -> None:
    # commits as a scalar → not treated as push; repository/pusher wrong types → no raise.
    assert gh_events.translate({"ref": "r", "commits": 5, "action": "x"}) == []
    out = gh_events.translate({"commits": [{}], "ref": "r", "repository": [], "pusher": "s"})
    assert out[0][0] == "vcs.push" and out[0][1]["repo"] == ""


# --- write-tool request bodies (the tools without lifecycle coverage) --------


def test_write_tool_request_bodies_are_correct() -> None:
    def sent(tool: str, args: dict[str, Any], status: int, resp_body: dict[str, Any]) -> Any:
        tx = gh_http.RecordedTransport()
        for method in ("POST", "PATCH", "GET"):
            tx.responses.append(
                (method, "", gh_http.HttpResponse(status, _fresh_headers(), json.dumps(resp_body)))
            )
        _connector(tx).call_tool(tool, args)
        return tx.calls[-1]

    m, url, _h, body = sent(
        "github.create_repo", {"org": "acme", "name": "svc", "private": True}, 201, {"id": 1}
    )
    assert m == "POST" and url.endswith("/orgs/acme/repos")
    assert json.loads(body) == {"name": "svc", "private": True}
    assert sent("github.create_repo", {"name": "svc"}, 201, {})[1].endswith("/user/repos")

    m, url, _h, body = sent(
        "github.update_pr", {"owner": "o", "repo": "r", "number": 5, "state": "closed"}, 200, {}
    )
    assert m == "PATCH" and url.endswith("/pulls/5") and json.loads(body) == {"state": "closed"}

    _m, url, _h, body = sent(
        "github.create_issue",
        {"owner": "o", "repo": "r", "title": "bug", "labels": ["p0"]},
        201,
        {},
    )
    assert url.endswith("/issues") and json.loads(body) == {"title": "bug", "labels": ["p0"]}

    _m, url, _h, body = sent(
        "github.comment", {"owner": "o", "repo": "r", "number": 9, "body": "hi"}, 201, {}
    )
    assert url.endswith("/issues/9/comments") and json.loads(body) == {"body": "hi"}

    m, url, _h, _b = sent(
        "github.search_code", {"owner": "o", "repo": "r", "q": "def foo"}, 200, {"items": []}
    )
    assert m == "GET" and "/search/code?q=def%20foo" in url


def test_lifecycle_request_bodies_carry_the_right_fields() -> None:
    tx = gh_http.RecordedTransport()
    h = _fresh_headers()
    tx.add("POST", "/git/refs", gh_http.HttpResponse(201, h, "{}"))
    tx.add("POST", "/pulls", gh_http.HttpResponse(201, h, json.dumps({"number": 3})))
    tx.add("PUT", "/merge", gh_http.HttpResponse(200, h, json.dumps({"merged": True})))
    conn = _connector(tx)
    conn.call_tool(
        "github.create_branch", {"owner": "o", "repo": "r", "branch": "feat", "sha": "s1"}
    )
    conn.call_tool(
        "github.open_pr", {"owner": "o", "repo": "r", "title": "T", "head": "feat", "base": "main"}
    )
    conn.call_tool(
        "github.merge_pr", {"owner": "o", "repo": "r", "number": 3, "merge_method": "rebase"}
    )
    branch_body, pr_body, merge_body = (json.loads(c[3]) for c in tx.calls)
    assert branch_body == {"ref": "refs/heads/feat", "sha": "s1"}  # refs/heads/ prefix
    assert pr_body == {"title": "T", "head": "feat", "base": "main"}
    assert merge_body == {"merge_method": "rebase"}


def test_tool_arguments_cannot_inject_a_different_path() -> None:
    tx = gh_http.RecordedTransport()
    tx.add(
        "GET",
        "/check-runs",
        gh_http.HttpResponse(200, _fresh_headers(), json.dumps({"check_runs": []})),
    )
    _connector(tx).call_tool(
        "github.get_checks", {"owner": "o/../evil", "repo": "r", "ref": "a?b#c"}
    )
    url = tx.calls[-1][1]
    assert "o%2F..%2Fevil" in url and "a%3Fb%23c" in url  # metacharacters percent-encoded
    assert "/commits/a?b" not in url  # not a real query separator


def test_secondary_rate_limit_429_honors_retry_after_header() -> None:
    rl: Any = load_connector("github", "github_ratelimit")
    tx = gh_http.RecordedTransport()
    # remaining > 0 (a secondary/abuse limit) but a Retry-After header is present.
    tx.add(
        "POST",
        "/pulls",
        gh_http.HttpResponse(429, {"x-ratelimit-remaining": "50", "retry-after": "42"}, "{}"),
    )
    client = gh_api.GithubClient(
        tx,
        gh_auth.GithubAuth({"GITHUB_TOKEN": "x"}, tx, api_url="https://api.github.com"),
        api_url="https://api.github.com",
        bucket=rl.RateBucket(),
    )
    out = client.call(
        "github.open_pr", {"owner": "o", "repo": "r", "title": "t", "head": "h", "base": "b"}
    )
    assert out["ok"] is False and out["error_code"] == "RATE_LIMITED" and out["retry_after"] == 42
