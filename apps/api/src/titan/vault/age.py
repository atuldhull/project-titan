"""A self-contained implementation of the ``age`` v1 file format, X25519 recipient
type (https://age-encryption.org/v1), built only on ``cryptography`` primitives.

Why hand-rolled rather than shelling out to the ``age``/``sops`` binaries: the
platform is "local-first and free" (SEC-002) and must run in plain Docker
Compose with no extra binaries in the image. ``cryptography`` (already a
dependency) ships every primitive age needs — X25519, HKDF-SHA256,
ChaCha20-Poly1305, scrypt — so the vault can unlock an age-encrypted YAML at
boot with zero infrastructure and store git-safe ciphertext (ADR-SEC-03).

Scope: exactly the X25519 recipient stanza and the ChaCha20-Poly1305 STREAM
payload. Files this vault writes are standard age files — an operator holding
the identity can decrypt them with the real ``age`` CLI, and files produced by
``age -r … -o secrets.yaml.age`` decrypt here. Passphrase (scrypt) recipients
and multi-recipient files are read only if the X25519 stanza is one of them; we
always WRITE a single X25519 recipient.

The format, precisely (what the MAC and nonces cover), is the subtle part and is
called out inline — get any of it wrong and it silently stops being age.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INTRO = b"age-encryption.org/v1"
_X25519_INFO = b"age-encryption.org/v1/X25519"
_HEADER_INFO = b"header"
_PAYLOAD_INFO = b"payload"
_FILE_KEY_BYTES = 16
_STREAM_CHUNK = 64 * 1024  # age STREAM plaintext chunk size
_NONCE_BYTES = 16  # payload STREAM salt written after the header
_MAC_HRP = "age-secret-key-"
_RECIPIENT_HRP = "age"


class AgeError(Exception):
    """A malformed age file or a wrong identity."""


# --- base64 (standard alphabet, NO padding — age is strict about this) --------


def _b64(raw: bytes) -> str:
    return base64.standard_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.standard_b64decode(text + pad)


# --- bech32 (BIP-173; age keys are bech32, NOT bech32m) -----------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i, gen in enumerate(generators):
            chk ^= gen if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convert_bits(data: bytes | list[int], frombits: int, tobits: int, pad: bool) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        raise AgeError("invalid bech32 padding")
    return ret


def _bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convert_bits(payload, 8, 5, True)
    checksum = _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


def _bech32_decode(text: str, expected_hrp: str) -> bytes:
    lowered = text.lower()
    pos = lowered.rfind("1")
    if pos < 1:
        raise AgeError("not a bech32 string")
    hrp, data_part = lowered[:pos], lowered[pos + 1 :]
    if hrp != expected_hrp:
        raise AgeError(f"unexpected bech32 hrp {hrp!r} (want {expected_hrp!r})")
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError as exc:
        raise AgeError("invalid bech32 character") from exc
    if not _bech32_verify(hrp, data):
        raise AgeError("bad bech32 checksum")
    return bytes(_convert_bits(data[:-6], 5, 8, False))


# --- identities / recipients --------------------------------------------------


def parse_identity(text: str) -> X25519PrivateKey:
    """Parse an ``AGE-SECRET-KEY-1…`` string into an X25519 private key."""
    raw = _bech32_decode(text.strip(), _MAC_HRP)
    if len(raw) != 32:
        raise AgeError("age identity is not 32 bytes")
    return X25519PrivateKey.from_private_bytes(raw)


def parse_recipient(text: str) -> X25519PublicKey:
    """Parse an ``age1…`` recipient string into an X25519 public key."""
    raw = _bech32_decode(text.strip(), _RECIPIENT_HRP)
    if len(raw) != 32:
        raise AgeError("age recipient is not 32 bytes")
    return X25519PublicKey.from_public_bytes(raw)


def _public_bytes(pub: X25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _private_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def recipient_str(identity: X25519PrivateKey) -> str:
    """The ``age1…`` recipient string for an identity's public key."""
    return _bech32_encode(_RECIPIENT_HRP, _public_bytes(identity.public_key()))


def identity_str(identity: X25519PrivateKey) -> str:
    """The ``AGE-SECRET-KEY-1…`` string (upper-cased, as the age CLI prints it)."""
    return _bech32_encode(_MAC_HRP, _private_bytes(identity)).upper()


def generate_identity() -> tuple[str, str]:
    """A fresh (identity, recipient) age keypair, both as their canonical strings."""
    priv = X25519PrivateKey.generate()
    return identity_str(priv), recipient_str(priv)


# --- HKDF helpers -------------------------------------------------------------


def _hkdf(ikm: bytes, salt: bytes | None, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=SHA256(), length=length, salt=salt, info=info).derive(ikm)


# --- encrypt ------------------------------------------------------------------


def encrypt(plaintext: bytes, recipient: X25519PublicKey) -> bytes:
    """Encrypt ``plaintext`` to a single X25519 recipient, producing a standard
    age v1 file (binary)."""
    file_key = os.urandom(_FILE_KEY_BYTES)

    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = _public_bytes(ephemeral.public_key())
    recipient_pub = _public_bytes(recipient)
    shared = ephemeral.exchange(recipient)
    if shared == bytes(32):
        raise AgeError("low-order recipient key")  # all-zero shared secret
    wrap_key = _hkdf(shared, ephemeral_pub + recipient_pub, _X25519_INFO)
    wrapped = ChaCha20Poly1305(wrap_key).encrypt(bytes(12), file_key, None)

    stanza = b"-> X25519 " + _b64(ephemeral_pub).encode() + b"\n" + _b64(wrapped).encode()
    header_no_mac = _INTRO + b"\n" + stanza + b"\n---"

    mac_key = _hkdf(file_key, None, _HEADER_INFO)
    mac = hmac.new(mac_key, header_no_mac, hashlib.sha256).digest()
    header = header_no_mac + b" " + _b64(mac).encode() + b"\n"

    nonce = os.urandom(_NONCE_BYTES)
    stream_key = _hkdf(file_key, nonce, _PAYLOAD_INFO)
    body = _stream_encrypt(stream_key, plaintext)
    return header + nonce + body


def _stream_encrypt(key: bytes, plaintext: bytes) -> bytes:
    aead = ChaCha20Poly1305(key)
    out = bytearray()
    total = len(plaintext)
    # An empty plaintext still emits one (final) chunk, matching age.
    chunks = range(0, max(total, 1), _STREAM_CHUNK)
    count = len(chunks)
    for index, start in enumerate(chunks):
        chunk = plaintext[start : start + _STREAM_CHUNK]
        last = index == count - 1
        out += aead.encrypt(_stream_nonce(index, last), chunk, None)
    return bytes(out)


# --- decrypt ------------------------------------------------------------------


def decrypt(ciphertext: bytes, identity: X25519PrivateKey) -> bytes:
    """Decrypt an age v1 file with ``identity``; raises :class:`AgeError` on a
    malformed file, a wrong key, or a failed header MAC / payload tag."""
    try:
        header_end = ciphertext.index(b"\n---")
    except ValueError as exc:
        raise AgeError("no age header") from exc
    # The footer line is: ---<space><b64 mac>\n
    footer_start = header_end + 1
    line_end = ciphertext.index(b"\n", footer_start)
    footer = ciphertext[footer_start:line_end]
    if not footer.startswith(b"--- "):
        raise AgeError("malformed age header footer")
    header_no_mac = ciphertext[:footer_start] + b"---"
    file_mac = _unb64(footer[4:].decode("ascii"))

    header_lines = ciphertext[: header_end + 1].split(b"\n")
    if header_lines[0] != _INTRO:
        raise AgeError("not an age v1 file")

    file_key = _unwrap_file_key(header_lines, identity)

    mac_key = _hkdf(file_key, None, _HEADER_INFO)
    expected = hmac.new(mac_key, header_no_mac, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, file_mac):
        raise AgeError("header MAC mismatch (wrong identity or tampered header)")

    payload = ciphertext[line_end + 1 :]
    nonce, body = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
    if len(nonce) != _NONCE_BYTES:
        raise AgeError("truncated age payload")
    stream_key = _hkdf(file_key, nonce, _PAYLOAD_INFO)
    return _stream_decrypt(stream_key, body)


def _unwrap_file_key(header_lines: list[bytes], identity: X25519PrivateKey) -> bytes:
    recipient_pub = _public_bytes(identity.public_key())
    index = 1
    errors = 0
    while index < len(header_lines):
        line = header_lines[index]
        if not line.startswith(b"-> "):
            index += 1
            continue
        parts = line[3:].split(b" ")
        stanza_type = parts[0]
        # A stanza body is the following line(s) until the next stanza / footer.
        body_lines: list[bytes] = []
        index += 1
        while index < len(header_lines) and not header_lines[index].startswith(b"-> "):
            body_lines.append(header_lines[index])
            index += 1
        if stanza_type != b"X25519" or len(parts) != 2:
            errors += 1
            continue  # a recipient type we do not handle — try the next stanza
        try:
            ephemeral_pub = _unb64(parts[1].decode("ascii"))
            wrapped = _unb64(b"".join(body_lines).decode("ascii"))
            shared = identity.exchange(X25519PublicKey.from_public_bytes(ephemeral_pub))
            if shared == bytes(32):
                raise AgeError("low-order key")
            wrap_key = _hkdf(shared, ephemeral_pub + recipient_pub, _X25519_INFO)
            return ChaCha20Poly1305(wrap_key).decrypt(bytes(12), wrapped, None)
        except Exception:  # noqa: BLE001 — this identity does not open this stanza
            errors += 1
            continue
    raise AgeError("no X25519 stanza could be opened with this identity")


def _stream_decrypt(key: bytes, body: bytes) -> bytes:
    aead = ChaCha20Poly1305(key)
    out = bytearray()
    encrypted_chunk = _STREAM_CHUNK + 16  # plaintext chunk + Poly1305 tag
    if not body:
        raise AgeError("empty age payload")
    offset = 0
    index = 0
    while offset < len(body):
        chunk = body[offset : offset + encrypted_chunk]
        offset += encrypted_chunk
        last = offset >= len(body)
        try:
            out += aead.decrypt(_stream_nonce(index, last), chunk, None)
        except Exception as exc:  # noqa: BLE001
            raise AgeError("payload chunk failed to decrypt") from exc
        index += 1
    return bytes(out)


def _stream_nonce(counter: int, last: bool) -> bytes:
    """age STREAM nonce: 11-byte big-endian counter + a 1-byte last-chunk flag."""
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


__all__ = [
    "AgeError",
    "decrypt",
    "encrypt",
    "generate_identity",
    "identity_str",
    "parse_identity",
    "parse_recipient",
    "recipient_str",
]
