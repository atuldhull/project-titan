"""Test helpers for the Phase-67 Authorization Code + PKCE login flow.

User login is no longer a password grant (TITAN-SEC-010): tests obtain tokens by
driving `/oauth/authorize` → `/oauth/token`, exactly as the BFF does. These
helpers keep that off the individual test bodies.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

from fastapi.testclient import TestClient

REDIRECT_URI = "http://localhost:3000/api/auth/callback"


def make_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) for S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def authorize(
    client: TestClient,
    email: str,
    password: str,
    *,
    totp_code: str | None = None,
    challenge: str | None = None,
    redirect_uri: str = REDIRECT_URI,
) -> Any:
    """POST /oauth/authorize; returns the raw httpx Response (caller asserts)."""
    body: dict[str, Any] = {
        "email": email,
        "password": password,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge": challenge or make_pkce()[1],
        "code_challenge_method": "S256",
    }
    if totp_code is not None:
        body["totp_code"] = totp_code
    return client.post("/api/v1/oauth/authorize", json=body)


def login(
    client: TestClient,
    email: str,
    password: str,
    *,
    totp_code: str | None = None,
) -> dict[str, Any]:
    """Full flow → the token pair. Asserts each leg succeeded."""
    verifier, challenge = make_pkce()
    authz = authorize(client, email, password, totp_code=totp_code, challenge=challenge)
    assert authz.status_code == 200, authz.text
    code = authz.json()["code"]

    exchanged = client.post(
        "/api/v1/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    body: dict[str, Any] = exchanged.json()
    return body
