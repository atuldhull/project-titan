"""JWT verifier unit tests (TITAN-SEC-011): no DB, no network."""

import datetime as dt

import jwt as pyjwt
import pytest

from titan.identity.tokens import KeyRing, TokenInvalid
from titan.settings import Settings


@pytest.fixture(scope="module")
def keyring() -> KeyRing:
    return KeyRing(Settings(_env_file=None))


def test_mint_and_verify_round_trip(keyring: KeyRing) -> None:
    token, jti = keyring.mint(sub="u1", roles=["owner"], scope="user", family_id="f1")
    claims = keyring.verify(token)
    assert claims["sub"] == "u1"
    assert claims["org"] == "titan"
    assert claims["roles"] == ["owner"]
    assert claims["scope"] == "user"
    assert claims["jti"] == jti
    assert claims["fam"] == "f1"
    assert claims["aud"] == "titan-api" and claims["iss"] == "titan"


def test_ttl_is_capped_at_15_minutes(keyring: KeyRing) -> None:
    token, _ = keyring.mint(sub="u1", roles=[], scope="user", ttl_seconds=86400)
    claims = keyring.verify(token)
    assert claims["exp"] - claims["iat"] <= 15 * 60


def test_alg_none_rejected(keyring: KeyRing) -> None:
    forged = pyjwt.encode(
        {
            "sub": "u1",
            "aud": "titan-api",
            "iss": "titan",
            "jti": "x",
            "iat": dt.datetime.now(dt.UTC),
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenInvalid):
        keyring.verify(forged)


def test_hs256_rejected(keyring: KeyRing) -> None:
    forged = pyjwt.encode(
        {
            "sub": "u1",
            "aud": "titan-api",
            "iss": "titan",
            "jti": "x",
            "iat": dt.datetime.now(dt.UTC),
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        },
        key="secret",
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalid):
        keyring.verify(forged)


def test_expired_rejected(keyring: KeyRing) -> None:
    token, _ = keyring.mint(sub="u1", roles=[], scope="user", ttl_seconds=-10)
    with pytest.raises(TokenInvalid):
        keyring.verify(token)


def test_wrong_audience_and_issuer_rejected(keyring: KeyRing) -> None:
    # aud/iss tested in isolation: same key material, different expectations.
    token, _ = keyring.mint(sub="u1", roles=[], scope="user")

    import base64

    pem_b64 = base64.b64encode(keyring._private_pem).decode()
    wrong_aud = KeyRing(
        Settings(jwt_private_key_b64=pem_b64, jwt_audience="other-api", _env_file=None)
    )
    with pytest.raises(TokenInvalid):
        wrong_aud.verify(token)
    wrong_iss = KeyRing(
        Settings(jwt_private_key_b64=pem_b64, jwt_issuer="other-iss", _env_file=None)
    )
    with pytest.raises(TokenInvalid):
        wrong_iss.verify(token)


def test_tampered_payload_rejected(keyring: KeyRing) -> None:
    token, _ = keyring.mint(sub="u1", roles=[], scope="user")
    header, payload, signature = token.split(".")
    import base64
    import json

    decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
    decoded["roles"] = ["owner"]
    tampered_payload = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(TokenInvalid):
        keyring.verify(f"{header}.{tampered_payload}.{signature}")
