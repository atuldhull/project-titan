"""Per-task proxy credential (Phase 23; TITAN-SEC-043 'authenticating proxy').

The Sandbox Controller mints a short-lived HMAC token that binds a sandbox to its
(project, task, agent) identity and the exact hosts it may reach (the task's git
remote + any Admin-approved hosts). The sandbox presents it to the forward proxy
as ``Proxy-Authorization: Basic base64("task:<token>")``; the proxy verifies the
signature + expiry offline (no DB round-trip on the hot path) and uses the claims
for allowlisting and audit attribution.

Stdlib-only (hmac/sha256) — no new dependency. The token is signed, not
encrypted: it carries no secrets, only identity + an allowlist the proxy would
enforce anyway, so tamper-evidence (not confidentiality) is what matters.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenClaims:
    """The verified identity + per-task allowlist a proxy request carries."""

    project_id: uuid.UUID
    task_id: uuid.UUID | None
    agent_id: str
    git_host: str | None
    approved_hosts: tuple[str, ...]
    expires_at: float


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes, secret: str) -> str:
    return _b64u(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def mint_task_token(
    secret: str,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    agent_id: str,
    git_host: str | None = None,
    approved_hosts: tuple[str, ...] = (),
    ttl_seconds: float = 3600.0,
    now: float | None = None,
) -> str:
    """Mint ``<payload>.<sig>``: the identity + allowlist the proxy will trust."""
    issued = time.time() if now is None else now
    body = {
        "p": str(project_id),
        "t": str(task_id) if task_id is not None else None,
        "a": agent_id,
        "g": git_host,
        "h": list(approved_hosts),
        "e": issued + ttl_seconds,
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64u(payload)}.{_sign(payload, secret)}"


def verify_task_token(secret: str, token: str, *, now: float | None = None) -> TokenClaims | None:
    """Return the claims if the signature is valid and unexpired, else None."""
    try:
        payload_b64, sig = token.split(".", 1)
        payload = _b64u_decode(payload_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        return None
    try:
        body = json.loads(payload)
        expires_at = float(body["e"])
        project_id = uuid.UUID(str(body["p"]))
    except (ValueError, KeyError, TypeError):
        return None
    if (time.time() if now is None else now) >= expires_at:
        return None
    task_raw = body.get("t")
    return TokenClaims(
        project_id=project_id,
        task_id=uuid.UUID(str(task_raw)) if task_raw else None,
        agent_id=str(body.get("a", "")),
        git_host=body.get("g"),
        approved_hosts=tuple(str(h) for h in (body.get("h") or [])),
        expires_at=expires_at,
    )


__all__ = ["TokenClaims", "mint_task_token", "verify_task_token"]
