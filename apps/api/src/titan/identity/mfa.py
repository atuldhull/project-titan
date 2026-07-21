"""TOTP multi-factor auth service (Phase 67; TITAN-SEC-015).

Pure service layer over the Phase-67 MFA tables — no FastAPI here; the router is
wired separately. The shape mirrors :mod:`titan.identity.service`: module-level
``async def`` functions taking a Core :class:`AsyncConnection`, raw ``text()``
SQL, and Pydantic DTOs across the module boundary.

Design notes worth carrying:

- **Enrollment is two-phase.** ``begin_enrollment`` stores an *unconfirmed*
  secret (``confirmed_at IS NULL``); MFA is not enforced and recovery codes are
  not minted until ``confirm_enrollment`` proves possession of the authenticator
  by verifying one live code. A half-finished enrollment therefore never locks
  anyone out, and a re-enroll only ever replaces a *pending* secret — an already
  confirmed credential is refused so re-enrolling can't silently disable MFA.
- **Recovery codes take the password path.** They are low-entropy typable
  strings, so they are Argon2id-hashed (like local passwords) rather than
  SHA-256'd (like high-entropy API keys), and consumed single-use via
  ``used_at`` (TITAN-SEC-015, matching the ``MfaRecoveryCode`` table contract).
- **Secrets stay out of logs.** The base32 secret and recovery plaintext are
  returned to the caller (shown once) and never logged.

TOTP REPLAY — accepted, documented limitation
----------------------------------------------
``pyotp``'s ``verify`` does not remember which 30-second timestep a user last
consumed, so within a single ``valid_window`` the *same* TOTP code can be
presented twice. Proper protection requires persisting the last-consumed
timestep per user, but the ``mfa_credentials`` table (alembic 0034) carries no
such column and altering the schema is out of scope for this service. The
residual exposure is one ~30-90 s window per code (bounded by ``valid_window``),
and is recorded here as a follow-up (a ``last_timestep`` column, or a Redis
``SETNX`` keyed by ``user_id:timestep``). The **recovery-code** path has no such
gap: those are genuinely single-use, enforced at the DB via ``used_at``.
"""

from __future__ import annotations

import base64
import datetime as dt
import secrets
import struct
import uuid
import zlib

import pyotp
import qrcode  # type: ignore[import-untyped]  # no py.typed marker; treated as Any
import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.identity import audit, passwords
from titan.identity.audit import SecurityEventType
from titan.vault.cipher import FieldCipher

logger = structlog.get_logger("titan.identity.mfa")


def _decrypt_secret(cipher: FieldCipher, stored: str) -> str:
    """Decrypt a stored TOTP secret (TITAN-SEC-063). Tolerates a legacy PLAINTEXT
    base32 secret written before the Phase-68 field-encryption rollout — those
    rows still verify, and re-enrolling re-writes them encrypted. A base32 secret
    never matches the ``v<n>.<b64>.<b64>`` ciphertext shape, so the check is
    unambiguous."""
    if cipher.is_ciphertext(stored):
        return cipher.decrypt_str(stored)
    logger.warning("mfa.legacy_plaintext_secret")  # pre-Phase-68 row
    return stored


# Recovery codes: three hyphen-separated groups of four characters over an
# unambiguous alphabet (no 0/O/1/I/L). 12 chars × log2(31) ≈ 59 bits — plenty
# for a single-use fallback — and readable/typable off a printed sheet.
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_RECOVERY_GROUPS = 3
_RECOVERY_GROUP_LEN = 4

# Privileged roles that org MFA policy applies to (§9.3, TITAN-SEC-015).
_PRIVILEGED_ROLES = frozenset({"owner", "admin"})


class MfaError(Exception):
    """Base class for MFA operations the caller should surface clearly."""


class MfaAlreadyEnrolled(MfaError):
    """Re-enrolling while a CONFIRMED credential exists — refused so a fresh
    enrollment can never silently disable working MFA."""


class MfaNotEnrolled(MfaError):
    """Confirming (or operating on) an enrollment that does not exist."""


class MfaCodeInvalid(MfaError):
    """A supplied TOTP code did not verify against the pending secret."""


class EnrollmentResult(BaseModel):
    """Returned by :func:`begin_enrollment` — everything the UI needs to show
    the authenticator setup step. ``secret`` is shown once for manual entry;
    ``qr_png_data_url`` is a self-contained ``data:image/png;base64,…`` image."""

    secret: str
    provisioning_uri: str
    qr_png_data_url: str


def _generate_recovery_codes() -> list[str]:
    """`RECOVERY_CODE_COUNT` crypto-random 'xxxx-xxxx-xxxx' codes."""
    codes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        groups = [
            "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_LEN))
            for _ in range(_RECOVERY_GROUPS)
        ]
        codes.append("-".join(groups))
    return codes


def _normalize_recovery(code: str) -> str:
    """Canonicalise a typed recovery code to match the stored form (the alphabet
    is uppercase-only, so uppercasing is loss-free)."""
    return code.strip().upper()


def _png_from_matrix(matrix: list[list[bool]], scale: int = 8) -> bytes:
    """Encode a QR module matrix as an 8-bit grayscale PNG using only the stdlib.

    Neither Pillow nor pypng is installed, so ``qrcode``'s own image factories
    are unavailable; we take the boolean module matrix (dark = True) and hand-roll
    a minimal PNG (IHDR/IDAT/IEND, filter type 0, zlib-deflated scanlines). Each
    module becomes a ``scale``×``scale`` block; the matrix already includes the
    quiet-zone border.
    """
    n_cols = len(matrix[0])
    width = n_cols * scale
    height = len(matrix) * scale

    raw = bytearray()
    for row in matrix:
        scan = bytearray()
        for dark in row:
            scan.extend((b"\x00" if dark else b"\xff") * scale)
        for _ in range(scale):
            raw.append(0)  # per-scanline filter type 0 (None)
            raw.extend(scan)
    compressed = zlib.compress(bytes(raw), 9)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # bit depth 8, colour type 0 (grayscale), default compression/filter/interlace
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _qr_png_data_url(uri: str) -> str:
    """Render `uri` to a base64 ``data:image/png`` URL (see :func:`_png_from_matrix`)."""
    qr = qrcode.QRCode(border=4, box_size=1)
    qr.add_data(uri)
    qr.make(fit=True)
    matrix: list[list[bool]] = qr.get_matrix()
    png = _png_from_matrix(matrix)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def begin_enrollment(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    account_label: str,
    issuer: str,
    cipher: FieldCipher,
) -> EnrollmentResult:
    """Start (or restart) TOTP enrollment: mint a fresh secret and UPSERT it
    unconfirmed. Refuses to touch an already CONFIRMED credential — recovery
    codes are NOT generated here (they come at confirmation)."""
    existing = (
        await conn.execute(
            text("SELECT confirmed_at FROM mfa_credentials WHERE user_id = :uid"),
            {"uid": user_id},
        )
    ).first()
    if existing is not None and existing.confirmed_at is not None:
        raise MfaAlreadyEnrolled("MFA is already active; disable it before re-enrolling")

    secret = pyotp.random_base32()
    # AES-256-GCM at rest (TITAN-SEC-063). A re-enroll (existing but unconfirmed)
    # replaces the pending secret and keeps confirmed_at NULL; the guard above
    # ensures we never clear a confirmed_at that mattered.
    await conn.execute(
        text(
            "INSERT INTO mfa_credentials (user_id, secret, confirmed_at) "
            "VALUES (:uid, :secret, NULL) "
            "ON CONFLICT (user_id) DO UPDATE SET secret = :secret, confirmed_at = NULL"
        ),
        {"uid": user_id, "secret": cipher.encrypt_str(secret)},
    )
    uri = pyotp.TOTP(secret).provisioning_uri(name=account_label, issuer_name=issuer)
    logger.info("mfa.enroll_begin", user_id=str(user_id))  # secret intentionally omitted
    return EnrollmentResult(
        secret=secret, provisioning_uri=uri, qr_png_data_url=_qr_png_data_url(uri)
    )


async def confirm_enrollment(
    conn: AsyncConnection, user_id: uuid.UUID, code: str, cipher: FieldCipher
) -> list[str]:
    """Verify `code` against the pending secret; on success confirm the
    credential, mint and store `RECOVERY_CODE_COUNT` Argon2id-hashed recovery
    codes, and RETURN their plaintext (shown once). Raises on a wrong code or a
    missing/already-confirmed enrollment — nothing is confirmed on failure."""
    row = (
        await conn.execute(
            text("SELECT secret, confirmed_at FROM mfa_credentials WHERE user_id = :uid"),
            {"uid": user_id},
        )
    ).first()
    if row is None:
        raise MfaNotEnrolled("no pending MFA enrollment to confirm")
    if row.confirmed_at is not None:
        raise MfaAlreadyEnrolled("MFA is already confirmed")

    # valid_window=1 tolerates ~±30 s of clock skew between server and app. An
    # undecryptable pending secret surfaces as MfaCodeInvalid (a 4xx), never a
    # 500 — confirmation just fails and the user re-enrolls.
    try:
        pending_secret = _decrypt_secret(cipher, row.secret)
    except ValueError as exc:
        raise MfaCodeInvalid("enrollment secret could not be read") from exc
    if not pyotp.TOTP(pending_secret).verify(code, valid_window=1):
        raise MfaCodeInvalid("invalid TOTP code")

    now = dt.datetime.now(dt.UTC)
    await conn.execute(
        text("UPDATE mfa_credentials SET confirmed_at = :now WHERE user_id = :uid"),
        {"now": now, "uid": user_id},
    )
    codes = _generate_recovery_codes()
    for plaintext in codes:
        await conn.execute(
            text(
                "INSERT INTO mfa_recovery_codes (id, user_id, code_hash) VALUES (:id, :uid, :hash)"
            ),
            {"id": uuid7(), "uid": user_id, "hash": passwords.hash_password(plaintext)},
        )
    await audit.write(conn, SecurityEventType.MFA_ENROLLED, user_id=user_id)
    logger.info("mfa.enroll_confirm", user_id=str(user_id))
    return codes


async def verify_totp(
    conn: AsyncConnection, user_id: uuid.UUID, code: str, cipher: FieldCipher
) -> bool:
    """Verify a login second factor. Tries the confirmed TOTP secret first, then
    falls back to unused recovery codes; a matching recovery code is burned
    (``used_at``). Returns True/False (never raises) and emits the matching audit
    event so the caller decides the 401.

    NOTE (TOTP replay): a fresh TOTP code that verifies here is NOT marked
    consumed — the schema has no last-timestep column (see module docstring), so
    the same code could be replayed within its `valid_window`. Accepted, tracked
    limitation. Recovery codes, by contrast, are genuinely single-use below.
    """
    secret_row = (
        await conn.execute(
            text(
                "SELECT secret FROM mfa_credentials "
                "WHERE user_id = :uid AND confirmed_at IS NOT NULL"
            ),
            {"uid": user_id},
        )
    ).first()
    # An undecryptable secret (a rotated/misconfigured field key, or a corrupt
    # blob) must NOT 500 and must NOT block the recovery-code fallback below —
    # fall through as a TOTP miss so a locked-out user can still use a recovery
    # code, and the caller returns a clean 401 rather than a server error.
    if secret_row is not None:
        try:
            totp_ok = pyotp.TOTP(_decrypt_secret(cipher, secret_row.secret)).verify(
                code, valid_window=1
            )
        except ValueError:
            logger.warning("mfa.secret_undecryptable", user_id=str(user_id))
            totp_ok = False
        if totp_ok:
            await audit.write(conn, SecurityEventType.MFA_VERIFIED, user_id=user_id)
            return True

    candidate = _normalize_recovery(code)
    unused = (
        await conn.execute(
            text(
                "SELECT id, code_hash FROM mfa_recovery_codes "
                "WHERE user_id = :uid AND used_at IS NULL"
            ),
            {"uid": user_id},
        )
    ).all()
    now = dt.datetime.now(dt.UTC)
    for rc in unused:
        if passwords.verify_password(rc.code_hash, candidate):
            # Atomic single-use burn: the `used_at IS NULL` guard + rowcount
            # closes a concurrent double-submit of the same recovery code.
            result = await conn.execute(
                text(
                    "UPDATE mfa_recovery_codes SET used_at = :now "
                    "WHERE id = :id AND used_at IS NULL"
                ),
                {"now": now, "id": rc.id},
            )
            if result.rowcount == 1:
                await audit.write(conn, SecurityEventType.MFA_RECOVERY_USED, user_id=user_id)
                return True
            break  # lost the race — already consumed; fail closed

    await audit.write(conn, SecurityEventType.MFA_FAILED, user_id=user_id)
    return False


async def is_mfa_enabled(conn: AsyncConnection, user_id: uuid.UUID) -> bool:
    """True iff the user has a CONFIRMED TOTP credential."""
    row = (
        await conn.execute(
            text("SELECT 1 FROM mfa_credentials WHERE user_id = :uid AND confirmed_at IS NOT NULL"),
            {"uid": user_id},
        )
    ).first()
    return row is not None


async def get_org_mfa_required(conn: AsyncConnection, org: str) -> bool:
    """Whether org policy mandates MFA for privileged roles (default False)."""
    row = (
        await conn.execute(
            text("SELECT mfa_required FROM org_security_policy WHERE org = :org"),
            {"org": org},
        )
    ).first()
    return bool(row.mfa_required) if row is not None else False


async def set_org_mfa_required(conn: AsyncConnection, org: str, required: bool) -> None:
    """UPSERT the org MFA policy; emits MFA_POLICY_CHANGED only on an actual
    change of the effective value."""
    current = await get_org_mfa_required(conn, org)
    await conn.execute(
        text(
            "INSERT INTO org_security_policy (org, mfa_required) VALUES (:org, :req) "
            "ON CONFLICT (org) DO UPDATE SET mfa_required = :req"
        ),
        {"org": org, "req": required},
    )
    if current != required:
        await audit.write(
            conn,
            SecurityEventType.MFA_POLICY_CHANGED,
            detail={"org": org, "mfa_required": required},
        )


async def mfa_required_for_login(
    conn: AsyncConnection, user_id: uuid.UUID, org: str, roles: list[str]
) -> bool:
    """Enforcement predicate (TITAN-SEC-015): True when org policy requires MFA
    AND the user holds a privileged (owner/admin) role — the login must then
    demand a TOTP code. Deliberately independent of enrollment state: a
    privileged user under policy with NO credential still returns True, which is
    the "force enrollment at next privileged login" case the caller acts on.

    `user_id` is part of the interface (a future per-user exemption/grace policy
    keys on it) though the current predicate is org+role only.
    """
    if not await get_org_mfa_required(conn, org):
        return False
    return any(role.lower() in _PRIVILEGED_ROLES for role in roles)


__all__ = [
    "EnrollmentResult",
    "MfaAlreadyEnrolled",
    "MfaCodeInvalid",
    "MfaError",
    "MfaNotEnrolled",
    "RECOVERY_CODE_COUNT",
    "begin_enrollment",
    "confirm_enrollment",
    "get_org_mfa_required",
    "is_mfa_enabled",
    "mfa_required_for_login",
    "set_org_mfa_required",
    "verify_totp",
]
