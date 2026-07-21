"""OAuth2/OIDC service layer (Phase 67; TITAN-SEC-010).

fakeredis, no DB — this exercises PKCE arithmetic, grant/response-type
rejection, and the single-use authorization-code store. Uses the repo's
``run_async`` helper rather than pytest-asyncio, matching the rest of the suite.
"""

from __future__ import annotations

import secrets

import fakeredis.aioredis
import pytest

from tests.conftest import run_async
from titan.identity.oidc import (
    AuthCodePayload,
    AuthCodeStore,
    OAuthError,
    compute_s256_challenge,
    discovery_document,
    validate_grant_type,
    validate_response_type,
    verify_pkce,
)


def _store(ttl_seconds: int = 60) -> AuthCodeStore:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return AuthCodeStore(redis, ttl_seconds=ttl_seconds)


def _payload(**overrides: object) -> AuthCodePayload:
    verifier = "a" * 64
    base: dict[str, object] = {
        "user_id": "user-123",
        "redirect_uri": "https://app.titan.local/callback",
        "code_challenge": compute_s256_challenge(verifier),
        "code_challenge_method": "S256",
        "scope": "openid profile",
        "roles": ["admin"],
    }
    base.update(overrides)
    return AuthCodePayload(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #
def test_verify_pkce_accepts_a_matching_s256_pair() -> None:
    verifier = secrets.token_urlsafe(48)  # 43-128 chars, ASCII
    challenge = compute_s256_challenge(verifier)
    assert verify_pkce(verifier, challenge, "S256") is True


def test_verify_pkce_challenge_has_no_padding() -> None:
    challenge = compute_s256_challenge("a" * 64)
    # BASE64URL with SHA-256 (32 bytes) is 43 chars, never '='-padded.
    assert "=" not in challenge
    assert len(challenge) == 43


def test_verify_pkce_rejects_a_wrong_verifier() -> None:
    verifier = "a" * 64
    challenge = compute_s256_challenge(verifier)
    assert verify_pkce("b" * 64, challenge, "S256") is False


def test_verify_pkce_rejects_plain_method() -> None:
    # 'plain' is disallowed by policy even when verifier == challenge.
    value = "a" * 64
    assert verify_pkce(value, value, "plain") is False


def test_verify_pkce_rejects_unknown_method() -> None:
    verifier = "a" * 64
    challenge = compute_s256_challenge(verifier)
    assert verify_pkce(verifier, challenge, "S512") is False


def test_verify_pkce_rejects_too_short_verifier() -> None:
    verifier = "a" * 42  # one under the RFC 7636 minimum of 43
    challenge = compute_s256_challenge(verifier)
    assert verify_pkce(verifier, challenge, "S256") is False


def test_verify_pkce_rejects_too_long_verifier() -> None:
    verifier = "a" * 129  # one over the RFC 7636 maximum of 128
    challenge = compute_s256_challenge(verifier)
    assert verify_pkce(verifier, challenge, "S256") is False


# --------------------------------------------------------------------------- #
# response_type / grant_type validation (SEC-010)
# --------------------------------------------------------------------------- #
def test_validate_response_type_accepts_code() -> None:
    validate_response_type("code")  # must not raise


def test_validate_response_type_rejects_token_implicit() -> None:
    with pytest.raises(OAuthError) as exc:
        validate_response_type("token")
    assert exc.value.error == "unsupported_response_type"
    assert "implicit" in exc.value.description.lower()


def test_validate_response_type_rejects_garbage() -> None:
    with pytest.raises(OAuthError) as exc:
        validate_response_type("code id_token")
    assert exc.value.error == "unsupported_response_type"


def test_validate_grant_type_accepts_authorization_code() -> None:
    validate_grant_type("authorization_code")  # must not raise


def test_validate_grant_type_rejects_password() -> None:
    with pytest.raises(OAuthError) as exc:
        validate_grant_type("password")
    assert exc.value.error == "unsupported_grant_type"
    # Names the grant and points at the first-party form.
    assert "password" in exc.value.description.lower()
    assert "/authorize" in exc.value.description


def test_validate_grant_type_rejects_implicit() -> None:
    with pytest.raises(OAuthError) as exc:
        validate_grant_type("implicit")
    assert exc.value.error == "unsupported_grant_type"


def test_validate_grant_type_rejects_client_credentials() -> None:
    with pytest.raises(OAuthError) as exc:
        validate_grant_type("client_credentials")
    assert exc.value.error == "unsupported_grant_type"


def test_oauth_error_to_dict_is_rfc6749_shape() -> None:
    err = OAuthError("invalid_request", "missing code_verifier")
    assert err.to_dict() == {
        "error": "invalid_request",
        "error_description": "missing code_verifier",
    }


# --------------------------------------------------------------------------- #
# AuthCodeStore
# --------------------------------------------------------------------------- #
def test_issue_then_consume_round_trips() -> None:
    store = _store()
    verifier = "a" * 64
    payload = _payload()

    async def go() -> AuthCodePayload:
        code = await store.issue(payload)
        return await store.consume(code, payload.redirect_uri, verifier)

    result = run_async(go)
    assert result == payload
    assert result.roles == ["admin"]
    assert result.user_id == "user-123"


def test_a_code_is_single_use() -> None:
    store = _store()
    verifier = "a" * 64
    payload = _payload()

    async def go() -> str:
        code = await store.issue(payload)
        await store.consume(code, payload.redirect_uri, verifier)
        # A replay of the same code must miss — it was deleted on first use.
        try:
            await store.consume(code, payload.redirect_uri, verifier)
        except OAuthError as exc:
            return exc.error
        return "no-error"

    assert run_async(go) == "invalid_grant"


def test_consume_rejects_a_mismatched_redirect_uri() -> None:
    store = _store()
    verifier = "a" * 64
    payload = _payload()

    async def go() -> str:
        code = await store.issue(payload)
        try:
            await store.consume(code, "https://evil.example/callback", verifier)
        except OAuthError as exc:
            return exc.error
        return "no-error"

    assert run_async(go) == "invalid_grant"


def test_consume_rejects_a_wrong_verifier_and_burns_the_code() -> None:
    """A PKCE failure must still burn the code: no retry with the right one."""
    store = _store()
    payload = _payload()  # challenge is for "a" * 64

    async def go() -> tuple[str, str]:
        code = await store.issue(payload)
        try:
            await store.consume(code, payload.redirect_uri, "b" * 64)
            first = "no-error"
        except OAuthError as exc:
            first = exc.error
        # Even the CORRECT verifier now fails — the code was deleted.
        try:
            await store.consume(code, payload.redirect_uri, "a" * 64)
            second = "no-error"
        except OAuthError as exc:
            second = exc.error
        return first, second

    first, second = run_async(go)
    assert first == "invalid_grant"
    assert second == "invalid_grant"


def test_consume_of_a_never_issued_code_is_invalid_grant() -> None:
    store = _store()

    async def go() -> str:
        try:
            await store.consume("never-issued", "https://app.titan.local/callback", "a" * 64)
        except OAuthError as exc:
            return exc.error
        return "no-error"

    assert run_async(go) == "invalid_grant"


def test_consume_of_an_expired_code_is_invalid_grant() -> None:
    """TTL expiry looks exactly like a missing code. Simulate by deleting."""
    store = _store(ttl_seconds=60)
    verifier = "a" * 64
    payload = _payload()

    async def go() -> str:
        code = await store.issue(payload)
        # Drop the key out from under the store to emulate TTL expiry.
        await store._redis.delete(store._key(code))  # noqa: SLF001
        try:
            await store.consume(code, payload.redirect_uri, verifier)
        except OAuthError as exc:
            return exc.error
        return "no-error"

    assert run_async(go) == "invalid_grant"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_discovery_document_excludes_password_and_token() -> None:
    doc = discovery_document(issuer="https://titan.local", base_url="https://titan.local")
    assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "password" not in doc["grant_types_supported"]
    assert doc["response_types_supported"] == ["code"]
    assert "token" not in doc["response_types_supported"]
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["id_token_signing_alg_values_supported"] == ["RS256"]


def test_discovery_document_endpoints_use_base_url() -> None:
    doc = discovery_document(issuer="https://titan.local", base_url="https://titan.local")
    assert doc["authorization_endpoint"] == "https://titan.local/api/v1/oauth/authorize"
    assert doc["token_endpoint"] == "https://titan.local/api/v1/oauth/token"
    assert doc["jwks_uri"] == "https://titan.local/api/v1/oauth/jwks"
