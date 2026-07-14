"""The first-party chat (Slack + Discord) connector (Phase 54; Vol 7 §6.3, TITAN-INT-042/011).

In-process, no network: the SHARED routing/template/backoff-retry pipeline (one pipeline for
both backends), Slack v0 signature verification, the interactivity → approval handler, the
Discord backend, and the slash/interaction handlers. The Slack-button → REAL-approval-resolved
→ blocked-call-proceeds end-to-end (with DB) is in test_connectors_api.py. Skips if absent
(INT-011)."""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

from tests.connector_loader import load_connector

notify: Any = load_connector("chat", "chat_notify")
slack: Any = load_connector("chat", "chat_slack")
discord: Any = load_connector("chat", "chat_discord")
chat_http: Any = load_connector("chat", "chat_http")
chat_plugin: Any = load_connector("chat", "chat_plugin")

_CFG = {
    "SLACK_BOT_TOKEN": "xoxb-1",
    "SLACK_SIGNING_SECRET": "ssecret",
    "SLACK_CHANNEL_MAP": json.dumps({"projA": "#a"}),
    "SLACK_DEFAULT_CHANNEL": "#general",
    "SLACK_TEMPLATES": json.dumps({"milestone.completed": "Milestone {name} done in {project}"}),
    "SLACK_USER_MAP": json.dumps({"U1": "11111111-1111-1111-1111-111111111111"}),
    "DISCORD_BOT_TOKEN": "bot-1",
    "DISCORD_CHANNEL_MAP": json.dumps({"projA": "999"}),
    "DISCORD_DEFAULT_CHANNEL": "111",
}


def _ok() -> Any:
    return chat_http.HttpResponse(200, {}, json.dumps({"ok": True}))


def _connector(transport: Any, **kw: Any) -> Any:
    return chat_plugin.ChatConnector(config=_CFG, http=transport, sleep=lambda _s: None, **kw)


# --- the shared pipeline: routing / templates / retry (TITAN-INT-042) --------


def test_channel_routing_per_project_with_default_and_override() -> None:
    router = notify.ChannelRouter.from_json(
        _CFG["SLACK_CHANNEL_MAP"], _CFG["SLACK_DEFAULT_CHANNEL"]
    )
    assert router.channel_for("projA") == "#a"
    assert router.channel_for("unknown") == "#general"  # default fallback
    assert router.channel_for("projA", "#override") == "#override"  # explicit override wins


def test_templates_render_per_event_and_fall_back_safely() -> None:
    renderer = notify.TemplateRenderer.from_json(_CFG["SLACK_TEMPLATES"])
    assert (
        renderer.render("milestone.completed", {"name": "M1", "project": "projA"})
        == "Milestone M1 done in projA"
    )
    # A missing template field or an untemplated topic never raises — generic fallback.
    assert renderer.render("milestone.completed", {"name": "M1"}).startswith(
        "[milestone.completed]"
    )
    assert renderer.render("unknown.topic", {"x": 1}) == "[unknown.topic] x=1"


def test_retry_backs_off_then_surfaces_a_permanent_failure() -> None:
    attempts = {"n": 0}

    def flaky() -> bool:
        attempts["n"] += 1
        return attempts["n"] >= 3  # fail twice, then succeed

    ok = notify.send_with_retry(flaky, sleep=lambda _s: None)
    assert ok.delivered is True and ok.attempts == 3

    def always_raise() -> bool:
        raise RuntimeError("network down")

    dead = notify.send_with_retry(always_raise, sleep=lambda _s: None)
    assert (
        dead.delivered is False and dead.attempts == notify.MAX_ATTEMPTS
    )  # 5 → surfaced, not raised
    assert "network down" in dead.detail


def test_post_message_routes_by_project_and_reports_delivery() -> None:
    tx = chat_http.FakeTransport().push(_ok())
    out = _connector(tx).call_tool("slack.post_message", {"text": "hi", "project_id": "projA"})
    assert out["ok"] is True and out["attempts"] == 1
    assert json.loads(tx.calls[-1][3])["channel"] == "#a"
    assert tx.calls[-1][2]["authorization"] == "Bearer xoxb-1"


def test_a_permanently_failing_send_is_surfaced_not_raised() -> None:
    # Slack returns {ok: false} every time → the tool reports delivered=False after 5 tries.
    tx = chat_http.FakeTransport()
    for _ in range(notify.MAX_ATTEMPTS):
        tx.push(
            chat_http.HttpResponse(200, {}, json.dumps({"ok": False, "error": "channel_not_found"}))
        )
    out = _connector(tx).call_tool("slack.post_message", {"text": "hi", "project_id": "projA"})
    assert out["ok"] is False and out["attempts"] == notify.MAX_ATTEMPTS


# --- Slack signature verification (happy + bad, the v0 scheme) ----------------


def test_slack_v0_signature_verification() -> None:
    body = urlencode({"payload": "{}"})
    ts = str(int(time.time()))
    good = "v0=" + hmac.new(b"ssecret", f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    assert slack.verify_slack_signature("ssecret", timestamp=ts, raw_body=body, signature=good)
    assert not slack.verify_slack_signature(
        "ssecret", timestamp=ts, raw_body=body, signature="v0=deadbeef"
    )
    assert not slack.verify_slack_signature(
        "", timestamp=ts, raw_body=body, signature=good
    )  # no secret
    stale = str(int(time.time()) - 10_000)
    stale_sig = (
        "v0=" + hmac.new(b"ssecret", f"v0:{stale}:{body}".encode(), hashlib.sha256).hexdigest()
    )
    assert not slack.verify_slack_signature(
        "ssecret", timestamp=stale, raw_body=body, signature=stale_sig
    )  # replay window


# --- interactivity → approval resolution (attribution) -----------------------


def test_interactivity_resolves_the_named_approval_attributed_to_the_mapped_user() -> None:
    seen: dict[str, str] = {}

    def resolver(project_id: str, approval_id: str, action: str, approver: str) -> dict[str, Any]:
        seen.update(project=project_id, approval=approval_id, action=action, approver=approver)
        return {"status": 200}

    conn = _connector(chat_http.FakeTransport(), resolve=resolver)
    payload = {"user": {"id": "U1"}, "actions": [{"value": "approve|proj-1|appr-9"}]}
    body = {"raw": urlencode({"payload": json.dumps(payload)})}
    out = conn.on_webhook("/hooks/slack/interactivity", body)

    assert out["handled"] is True and out["action"] == "approved"
    assert seen["approval"] == "appr-9" and seen["action"] == "approved"
    assert seen["approver"] == "11111111-1111-1111-1111-111111111111"  # U1 → mapped TITAN user


def test_reject_button_from_a_mapped_approver_denies() -> None:
    seen: dict[str, str] = {}

    def resolver(project_id: str, approval_id: str, action: str, approver: str) -> dict[str, Any]:
        seen.update(action=action, approver=approver)
        return {"status": 200}

    conn = _connector(chat_http.FakeTransport(), resolve=resolver)
    payload = {"user": {"id": "U1"}, "actions": [{"value": "reject|p|a"}]}
    conn.on_webhook(
        "/hooks/slack/interactivity", {"raw": urlencode({"payload": json.dumps(payload)})}
    )
    assert seen["action"] == "denied"
    assert seen["approver"] == "11111111-1111-1111-1111-111111111111"


def test_an_unauthorized_slack_user_cannot_resolve_an_approval() -> None:
    # Reaching the (signed) endpoint is not enough — an UNMAPPED Slack user is not an
    # authorized approver, so the gate is NOT resolved (no default-approver fallback).
    calls: list[Any] = []

    def resolver(*a: Any) -> dict[str, Any]:
        calls.append(a)
        return {"status": 200}

    conn = _connector(chat_http.FakeTransport(), resolve=resolver)
    payload = {"user": {"id": "STRANGER"}, "actions": [{"value": "approve|p|a"}]}
    out = conn.on_webhook(
        "/hooks/slack/interactivity", {"raw": urlencode({"payload": json.dumps(payload)})}
    )
    assert out["handled"] is False and "authorized approver" in out["reason"]
    assert calls == []  # resolve was NEVER called


def test_post_approval_button_value_roundtrips_to_parse_interactivity() -> None:
    # The Approve button value post_approval emits must parse back to the same approval — a
    # format drift between approval_blocks and parse_interactivity would break the flow.
    tx = chat_http.FakeTransport().push(_ok())
    _connector(tx).call_tool(
        "slack.post_approval",
        {"text": "Approve merge?", "project_id": "proj-7", "approval_id": "appr-3"},
    )
    blocks = json.loads(tx.calls[-1][3])["blocks"]
    approve_value = blocks[1]["elements"][0]["value"]
    parsed = slack.parse_interactivity(
        {"user": {"id": "U1"}, "actions": [{"value": approve_value}]}
    )
    assert parsed == ("approve", "proj-7", "appr-3", "U1")


def test_non_approval_interactivity_is_ignored() -> None:
    conn = _connector(chat_http.FakeTransport(), resolve=lambda *a: {"status": 200})
    payload = {"user": {"id": "U1"}, "actions": [{"value": "some_other_button"}]}
    out = conn.on_webhook(
        "/hooks/slack/interactivity", {"raw": urlencode({"payload": json.dumps(payload)})}
    )
    assert out["handled"] is False


# --- Discord reuses the shared machinery (TITAN-INT-042) ---------------------


def test_discord_post_message_and_embed_route_through_shared_pipeline() -> None:
    tx = (
        chat_http.FakeTransport()
        .push(chat_http.HttpResponse(200, {}, "{}"))
        .push(chat_http.HttpResponse(200, {}, "{}"))
    )
    conn = _connector(tx)
    msg = conn.call_tool("discord.post_message", {"content": "hey", "project_id": "projA"})
    embed = conn.call_tool(
        "discord.post_embed", {"title": "T", "description": "D", "project_id": "unknown"}
    )
    assert msg["ok"] and "/channels/999/messages" in tx.calls[0][1]  # projA → 999
    assert embed["ok"] and "/channels/111/messages" in tx.calls[1][1]  # default → 111
    assert json.loads(tx.calls[1][3])["embeds"][0]["title"] == "T"


def test_chat_notify_renders_template_and_delivers_to_discord_backend() -> None:
    tx = chat_http.FakeTransport().push(chat_http.HttpResponse(200, {}, "{}"))
    out = _connector(tx).call_tool(
        "chat.notify",
        {
            "topic": "milestone.completed",
            "payload": {"name": "M1", "project": "projA"},
            "project_id": "projA",
            "backend": "discord",
        },
    )
    assert out["ok"] is True
    assert json.loads(tx.calls[-1][3])["content"] == "Milestone M1 done in projA"


def test_discord_ping_pongs_and_command_replies_ephemerally() -> None:
    assert (
        discord.handle_interaction({"type": 1}, status_of=lambda _p: "x")["type"] == 1
    )  # PING → PONG
    cmd = discord.handle_interaction(
        {"type": 2, "data": {"options": [{"name": "project", "value": "projA"}]}},
        status_of=lambda p: f"status:{p}",
    )
    assert cmd["type"] == 4 and cmd["data"]["flags"] == 64 and "projA" in cmd["data"]["content"]


def test_discord_signature_verification_uses_the_injected_ed25519_verifier() -> None:
    calls: list[tuple[str, bytes, str]] = []

    def verifier(pub: str, message: bytes, sig: str) -> bool:
        calls.append((pub, message, sig))
        return sig == "good"

    assert discord.verify_discord_signature(
        "PUB", timestamp="1", raw_body="{}", signature="good", verifier=verifier
    )
    assert not discord.verify_discord_signature(
        "PUB", timestamp="1", raw_body="{}", signature="bad", verifier=verifier
    )
    assert calls[0][1] == b"1{}"  # signed over timestamp + body


# --- slash command -----------------------------------------------------------


def test_slack_slash_status_command() -> None:
    conn = _connector(chat_http.FakeTransport())
    out = conn.on_webhook(
        "/hooks/slack/commands", {"raw": urlencode({"command": "/titan", "text": "status projA"})}
    )
    assert out["response_type"] == "ephemeral" and "projA" in out["text"]
