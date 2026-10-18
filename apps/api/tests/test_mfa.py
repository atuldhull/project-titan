"""TOTP MFA service tests (Phase 67; TITAN-SEC-015) — needs the disposable test
DB (schema marker). Exercises the pure service layer in
``titan.identity.mfa`` directly against Postgres, no HTTP: enrollment →
confirmation (with real pyotp codes) → login verification → recovery-code
single-use → org policy + the forced-enrollment predicate."""

import base64
import time
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

_ISSUER = "TITAN"
# A deterministic cipher so an enroll-then-verify round-trip decrypts (SEC-063).
_CIPHER = FieldCipher("mfa-test-key")


async def _new_user(engine: Any) -> uuid.UUID:
    """Seed one local user (unique email — the schema persists across the session)."""
    uid = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'MFA User', 'local', :s)"
            ),
            {"id": uid, "e": f"mfa-{uid.hex[:12]}@titan.local", "s": str(uid)},
        )
    return uid


async def _enroll(engine: Any, uid: uuid.UUID) -> str:
    async with engine.begin() as conn:
        result = await mfa.begin_enrollment(
            conn, uid, f"{uid.hex[:8]}@titan.local", _ISSUER, _CIPHER
        )
    return result.secret


async def _confirm(engine: Any, uid: uuid.UUID, secret: str) -> list[str]:
    async with engine.begin() as conn:
        return await mfa.confirm_enrollment(conn, uid, pyotp.TOTP(secret).now(), _CIPHER)


def _wrong_code(secret: str) -> str:
    """A 6-digit code guaranteed to miss the whole valid_window=1 window."""
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    live = {totp.at(now - 30), totp.at(now), totp.at(now + 30)}
    return next(c for c in ("000000", "111111", "222222", "333333", "444444") if c not in live)


def test_begin_enrollment_produces_secret_uri_and_png_qr(migrated_engine: Any) -> None:
    async def go() -> mfa.EnrollmentResult:
        uid = await _new_user(migrated_engine)
        async with migrated_engine.begin() as conn:
            return await mfa.begin_enrollment(conn, uid, "person@titan.local", _ISSUER, _CIPHER)

    result = run_async(go)
    assert result.secret and result.secret.isalnum()
    assert result.provisioning_uri.startswith("otpauth://totp/")
    assert "issuer=TITAN" in result.provisioning_uri
    # A real, decodable PNG data URL (built from the QR matrix via the stdlib).
    assert result.qr_png_data_url.startswith("data:image/png;base64,")
    png = base64.b64decode(result.qr_png_data_url.split(",", 1)[1])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_confirm_with_correct_code_returns_recovery_codes_and_enables(
    migrated_engine: Any,
) -> None:
    async def go() -> tuple[list[str], bool, int]:
        uid = await _new_user(migrated_engine)
        secret = await _enroll(migrated_engine, uid)
        async with migrated_engine.begin() as conn:
            codes = await mfa.confirm_enrollment(conn, uid, pyotp.TOTP(secret).now(), _CIPHER)
        async with migrated_engine.begin() as conn:
            enabled = await mfa.is_mfa_enabled(conn, uid)
            audited = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM security_events "
                        "WHERE user_id = :u AND event_type = 'mfa.enrolled'"
                    ),
                    {"u": uid},
                )
            ).scalar_one()
        return codes, enabled, audited

    codes, enabled, audited = run_async(go)
    assert len(codes) == mfa.RECOVERY_CODE_COUNT == 10
    assert len(set(codes)) == 10  # all distinct
    assert all(len(c) == 14 and c.count("-") == 2 for c in codes)  # xxxx-xxxx-xxxx
    assert enabled is True
    assert audited == 1  # MFA_ENROLLED written through the caller's conn


def test_confirm_with_wrong_code_raises_and_does_not_confirm(migrated_engine: Any) -> None:
    async def go() -> tuple[bool, bool]:
        uid = await _new_user(migrated_engine)
        secret = await _enroll(migrated_engine, uid)
        raised = False
        try:
            async with migrated_engine.begin() as conn:
                await mfa.confirm_enrollment(conn, uid, _wrong_code(secret), _CIPHER)
        except mfa.MfaCodeInvalid:
            raised = True
        async with migrated_engine.begin() as conn:
            enabled = await mfa.is_mfa_enabled(conn, uid)
        return raised, enabled

    raised, enabled = run_async(go)
    assert raised is True
    assert enabled is False  # nothing confirmed on a bad code


def test_verify_totp_accepts_fresh_rejects_wrong(migrated_engine: Any) -> None:
    async def go() -> tuple[bool, bool]:
        uid = await _new_user(migrated_engine)
        secret = await _enroll(migrated_engine, uid)
        await _confirm(migrated_engine, uid, secret)
        async with migrated_engine.begin() as conn:
            good = await mfa.verify_totp(conn, uid, pyotp.TOTP(secret).now(), _CIPHER)
        async with migrated_engine.begin() as conn:
            bad = await mfa.verify_totp(conn, uid, _wrong_code(secret), _CIPHER)
        return good, bad

    good, bad = run_async(go)
    assert good is True
    assert bad is False


def test_recovery_code_works_once_then_is_rejected(migrated_engine: Any) -> None:
    async def go() -> tuple[bool, bool]:
        uid = await _new_user(migrated_engine)
        secret = await _enroll(migrated_engine, uid)
        codes = await _confirm(migrated_engine, uid, secret)
        rc = codes[0]
        async with migrated_engine.begin() as conn:
            first = await mfa.verify_totp(conn, uid, rc, _CIPHER)
        async with migrated_engine.begin() as conn:
            second = await mfa.verify_totp(conn, uid, rc, _CIPHER)
        return first, second

    first, second = run_async(go)
    assert first is True  # consumed once
    assert second is False  # used_at now set — single-use enforced


def test_re_enroll_when_already_confirmed_raises(migrated_engine: Any) -> None:
    async def go() -> bool:
        uid = await _new_user(migrated_engine)
        secret = await _enroll(migrated_engine, uid)
        await _confirm(migrated_engine, uid, secret)
        try:
            async with migrated_engine.begin() as conn:
                await mfa.begin_enrollment(conn, uid, "again@titan.local", _ISSUER, _CIPHER)
        except mfa.MfaAlreadyEnrolled:
            return True
        return False

    assert run_async(go) is True  # re-enroll must not silently disable active MFA


def test_org_policy_roundtrip_and_required_predicate(migrated_engine: Any) -> None:
    async def go() -> dict[str, bool]:
        org = f"org-{uuid7().hex[-12:]}"  # unique — policy rows persist across the session
        owner = await _new_user(migrated_engine)
        viewer = await _new_user(migrated_engine)
        admin = await _new_user(migrated_engine)  # privileged but never enrolls

        async with migrated_engine.begin() as conn:
            before = await mfa.get_org_mfa_required(conn, org)
            await mfa.set_org_mfa_required(conn, org, True)
        async with migrated_engine.begin() as conn:
            after_on = await mfa.get_org_mfa_required(conn, org)
            owner_req = await mfa.mfa_required_for_login(conn, owner, org, ["owner"])
            viewer_req = await mfa.mfa_required_for_login(conn, viewer, org, ["viewer"])
            admin_req = await mfa.mfa_required_for_login(conn, admin, org, ["admin"])
            admin_enabled = await mfa.is_mfa_enabled(conn, admin)
        async with migrated_engine.begin() as conn:
            await mfa.set_org_mfa_required(conn, org, False)
            after_off = await mfa.get_org_mfa_required(conn, org)
        return {
            "before": before,
            "after_on": after_on,
            "after_off": after_off,
            "owner_req": owner_req,
            "viewer_req": viewer_req,
            "admin_req": admin_req,
            "admin_enabled": admin_enabled,
        }

    r = run_async(go)
    assert r["before"] is False and r["after_on"] is True and r["after_off"] is False  # round-trip
    assert r["owner_req"] is True  # privileged + policy → demand MFA
    assert r["viewer_req"] is False  # unprivileged → never forced
    assert r["admin_req"] is True and r["admin_enabled"] is False  # forced-enrollment case
