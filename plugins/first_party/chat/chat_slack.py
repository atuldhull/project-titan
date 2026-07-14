"""Slack backend (Vol 7 §6.3, TITAN-INT-042): outbound chat.postMessage + a Block Kit
approval message, plus inbound request-signature verification, the interactivity handler
that resolves a pending admin-gate approval, and the ``/titan status`` slash command.

Signature verification follows Slack's v0 scheme: HMAC-SHA256 over ``v0:{timestamp}:{body}``
with the signing secret, constant-time compared, with a 5-minute replay window. The
approval RESOLUTION goes through an injected ``resolve`` callable — in production a call to
the core tool-approval REST endpoint, in tests the core resolver directly — so the connector
needs no privileged approval capability of its own (zero core changes)."""

import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any

from chat_http import HttpTransport

SLACK_API = "https://slack.com/api"
_REPLAY_WINDOW_S = 300

#: (project_id, approval_id, action, approver_id) -> a result dict. ``action`` is the core
#: verb 'approved' | 'denied'.
ApprovalResolver = Callable[[str, str, str, str], dict[str, Any]]


def verify_slack_signature(
    signing_secret: str,
    *,
    timestamp: str,
    raw_body: str,
    signature: str,
    now: float | None = None,
) -> bool:
    """True iff ``signature`` (``v0=<hex>``) matches HMAC-SHA256(secret, ``v0:ts:body``) and
    the timestamp is within the replay window. No secret / stale / malformed → False."""
    if not signing_secret or not timestamp or not signature.startswith("v0="):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > _REPLAY_WINDOW_S:
        return False
    basestring = f"v0:{timestamp}:{raw_body}".encode()
    expected = (
        "v0="
        + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


# --- outbound ---------------------------------------------------------------


def post_message(
    transport: HttpTransport, token: str, *, channel: str, text: str
) -> dict[str, Any]:
    return _slack_post(
        transport, token, "chat.postMessage", {"channel": channel, "text": text}
    )


def approval_blocks(
    text: str, *, project_id: str, approval_id: str
) -> list[dict[str, Any]]:
    """A Block Kit message with Approve/Reject buttons whose value carries the approval it
    resolves (parsed back out by :func:`parse_interactivity`)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": f"titan_approval:{approval_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "titan_approve",
                    "value": f"approve|{project_id}|{approval_id}",
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "action_id": "titan_reject",
                    "value": f"reject|{project_id}|{approval_id}",
                },
            ],
        },
    ]


def post_approval(
    transport: HttpTransport,
    token: str,
    *,
    channel: str,
    text: str,
    project_id: str,
    approval_id: str,
) -> dict[str, Any]:
    return _slack_post(
        transport,
        token,
        "chat.postMessage",
        {
            "channel": channel,
            "text": text,
            "blocks": approval_blocks(
                text, project_id=project_id, approval_id=approval_id
            ),
        },
    )


def _slack_post(
    transport: HttpTransport, token: str, method: str, payload: dict[str, Any]
) -> dict[str, Any]:
    import json

    resp = transport.request(
        "POST",
        f"{SLACK_API}/{method}",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        body=json.dumps(payload),
    )
    data = resp.json()
    # Slack always returns 200 with an {ok: bool} body; a False ok is a failed send.
    return {
        "ok": bool(isinstance(data, dict) and data.get("ok")),
        "status": resp.status,
        "data": data,
    }


# --- inbound: interactivity (approval buttons) + slash ----------------------


def parse_interactivity(payload: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """A Slack interactivity payload → (action, project_id, approval_id, slack_user_id), or
    None if it is not a titan approval button. ``action`` is 'approve' | 'reject'."""
    actions = payload.get("actions") or []
    if not actions:
        return None
    value = str((actions[0] or {}).get("value", ""))
    parts = value.split(
        "|", 2
    )  # split into at most 3: an id may (in theory) contain '|'
    if len(parts) != 3 or parts[0] not in ("approve", "reject"):
        return None
    slack_user = str((payload.get("user") or {}).get("id", ""))
    return parts[0], parts[1], parts[2], slack_user


def handle_interactivity(
    payload: dict[str, Any],
    *,
    resolve: ApprovalResolver,
    user_map: dict[str, str],
) -> dict[str, Any]:
    """Resolve the pending approval named by the clicked button — but ONLY if the clicking
    Slack user is an AUTHORIZED approver (present in ``user_map``, which maps them to the
    TITAN user the resolution is attributed to). An unmapped user is refused: resolving an
    admin gate is a privileged act, so it is not enough to merely reach the (signed) endpoint
    — the clicker must be a recognized approver."""
    parsed = parse_interactivity(payload)
    if parsed is None:
        return {"handled": False, "reason": "not a titan approval action"}
    action, project_id, approval_id, slack_user = parsed
    approver = user_map.get(slack_user)
    if not approver:
        return {"handled": False, "reason": "slack user is not an authorized approver"}
    verb = "approved" if action == "approve" else "denied"
    result = resolve(project_id, approval_id, verb, approver)
    return {
        "handled": True,
        "action": verb,
        "approval_id": approval_id,
        "approver": approver,
        "slack_user": slack_user,
        "result": result,
    }


def handle_slash(
    form: dict[str, str], *, status_of: Callable[[str], str]
) -> dict[str, Any]:
    """``/titan status <project>`` → an ephemeral text reply from ``status_of(project)``."""
    text = (form.get("text") or "").strip()
    verb, _, arg = text.partition(" ")
    if verb == "status":
        return {"response_type": "ephemeral", "text": status_of(arg.strip())}
    return {"response_type": "ephemeral", "text": "usage: /titan status <project>"}
