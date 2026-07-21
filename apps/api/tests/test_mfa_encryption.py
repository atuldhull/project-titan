"""MFA TOTP secret encrypted at rest (Phase 68, TITAN-SEC-063). A pg_dump of
mfa_credentials.secret shows ciphertext, never the base32 secret — and a legacy
plaintext row (pre-Phase-68) still verifies."""

import uuid
from typing import Any

import pyotp
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.db import uuid7
from titan.identity import mfa
from titan.vault.cipher import FieldCipher

pytestmark = pytest.mark.schema

_CIPHER = FieldCipher("mfa-sec063-key")


async def _new_user(engine: Any) -> uuid.UUID:
    uid = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'U', 'local', :s)"
            ),
            {"id": uid, "e": f"sec063-{uid.hex[:12]}@titan.local", "s": str(uid)},
        )
    return uid


def test_stored_secret_is_ciphertext_not_base32(migrated_engine: Any) -> None:
    async def go() -> tuple[str, str, bool]:
        uid = await _new_user(migrated_engine)
        async with migrated_engine.begin() as conn:
            result = await mfa.begin_enrollment(conn, uid, "a@b.c", "TITAN", _CIPHER)
        async with migrated_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT secret FROM mfa_credentials WHERE user_id = :u"), {"u": uid}
                )
            ).scalar_one()
        # Confirm + verify still work against the ENCRYPTED column.
        async with migrated_engine.begin() as conn:
            await mfa.confirm_enrollment(conn, uid, pyotp.TOTP(result.secret).now(), _CIPHER)
        async with migrated_engine.begin() as conn:
            ok = await mfa.verify_totp(conn, uid, pyotp.TOTP(result.secret).now(), _CIPHER)
        return result.secret, stored, ok

    plaintext_secret, stored, ok = run_async(go)
    assert stored.startswith("v1.")  # SEC-063 ciphertext blob, not base32
    assert plaintext_secret not in stored  # the base32 secret is NOT on disk
    assert ok is True  # decrypt-on-read verifies correctly


def test_undecryptable_secret_does_not_500_and_recovery_code_still_works(
    migrated_engine: Any,
) -> None:
    # FIX B: a wrong/rotated field key makes the stored TOTP secret undecryptable.
    # verify_totp must NOT raise (no 500) and must fall through to the recovery
    # codes, which stay usable — a decrypt failure never locks a user out.
    async def go() -> tuple[bool, bool]:
        uid = await _new_user(migrated_engine)
        async with migrated_engine.begin() as conn:
            result = await mfa.begin_enrollment(conn, uid, "a@b.c", "TITAN", _CIPHER)
        async with migrated_engine.begin() as conn:
            codes = await mfa.confirm_enrollment(
                conn, uid, pyotp.TOTP(result.secret).now(), _CIPHER
            )
        # A DIFFERENT key: the stored ciphertext will not decrypt under it.
        wrong = FieldCipher("a-totally-different-key")
        async with migrated_engine.begin() as conn:
            totp_result = await mfa.verify_totp(
                conn, uid, pyotp.TOTP(result.secret).now(), wrong
            )  # must return False, not raise
        async with migrated_engine.begin() as conn:
            recovery_ok = await mfa.verify_totp(conn, uid, codes[0], wrong)
        return totp_result, recovery_ok

    totp_result, recovery_ok = run_async(go)
    assert totp_result is False  # undecryptable secret → clean miss, no 500
    assert recovery_ok is True  # recovery-code fallback still reachable


def test_legacy_plaintext_secret_still_verifies(migrated_engine: Any) -> None:
    # A row written before Phase 68 holds a raw base32 secret; verification must
    # tolerate it (and a re-enroll re-writes it encrypted).
    async def go() -> bool:
        uid = await _new_user(migrated_engine)
        secret = pyotp.random_base32()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO mfa_credentials (user_id, secret, confirmed_at) "
                    "VALUES (:u, :s, now())"
                ),
                {"u": uid, "s": secret},  # deliberately PLAINTEXT (legacy)
            )
        async with migrated_engine.begin() as conn:
            return await mfa.verify_totp(conn, uid, pyotp.TOTP(secret).now(), _CIPHER)

    assert run_async(go) is True
