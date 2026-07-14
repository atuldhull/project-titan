"""Discord backend (Vol 7 §6.3). Deliberately THIN — it reuses the shared
:mod:`chat_notify` routing/template/retry pipeline (no second notification pipeline). Adds
only Discord's wire format (bot messages + embeds) and its interactions endpoint.

Discord signs interactions with Ed25519 (``X-Signature-Ed25519`` over ``timestamp+body``),
which stdlib cannot verify, so the verify is an injected seam (default: the ``cryptography``
extra) — the PING→PONG + command handling is tested with a fake verifier."""

from collections.abc import Callable
from typing import Any

from chat_http import HttpTransport

DISCORD_API = "https://discord.com/api/v10"

#: (public_key_hex, message_bytes, signature_hex) -> valid?
Ed25519Verifier = Callable[[str, bytes, str], bool]

# Discord interaction + response type numbers (from the Interactions API).
_PING = 1
_APPLICATION_COMMAND = 2
_PONG = 1
_CHANNEL_MESSAGE = 4


def post_message(
    transport: HttpTransport, token: str, *, channel_id: str, content: str
) -> dict[str, Any]:
    return _post(transport, token, channel_id, {"content": content})


def post_embed(
    transport: HttpTransport,
    token: str,
    *,
    channel_id: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    return _post(
        transport,
        token,
        channel_id,
        {"embeds": [{"title": title, "description": description}]},
    )


def _post(
    transport: HttpTransport, token: str, channel_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    import json

    resp = transport.request(
        "POST",
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"authorization": f"Bot {token}", "content-type": "application/json"},
        body=json.dumps(payload),
    )
    return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": resp.json()}


def _default_verifier(
    public_key: str, message: bytes, signature: str
) -> bool:  # pragma: no cover
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature), message
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_discord_signature(
    public_key: str,
    *,
    timestamp: str,
    raw_body: str,
    signature: str,
    verifier: Ed25519Verifier | None = None,
) -> bool:
    if not (public_key and timestamp and signature):
        return False
    verify = verifier or _default_verifier
    return verify(public_key, f"{timestamp}{raw_body}".encode(), signature)


def handle_interaction(
    payload: dict[str, Any], *, status_of: Callable[[str], str]
) -> dict[str, Any]:
    """PING → PONG (the endpoint's liveness handshake); a ``/titan status`` command → an
    ephemeral channel message. Signature verification is the caller's responsibility."""
    kind = payload.get("type")
    if kind == _PING:
        return {"type": _PONG}
    if kind == _APPLICATION_COMMAND:
        data = payload.get("data") or {}
        options = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
        project = str(options.get("project", ""))
        return {
            "type": _CHANNEL_MESSAGE,
            "data": {"flags": 64, "content": status_of(project)},  # 64 = ephemeral
        }
    return {"type": _PONG}
