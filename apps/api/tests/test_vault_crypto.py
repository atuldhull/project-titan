"""Vault crypto primitives — field cipher, age, and the envelope signer
(Phase 68). No DB or infra: pure crypto round-trips and the negative cases that
must fail closed."""

import pytest

from titan.vault import age
from titan.vault.cipher import ConfigCipher, FieldCipher, verify_hmac_sha256
from titan.vault.signing import HmacEnvelopeSigner

# --- FieldCipher (SEC-063) ---------------------------------------------------


def test_field_cipher_str_and_dict_roundtrip() -> None:
    cipher = FieldCipher("a-stable-key")
    assert cipher.decrypt_str(cipher.encrypt_str("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"
    assert cipher.decrypt(cipher.encrypt({"x": 1, "y": "z"})) == {"x": 1, "y": "z"}


def test_field_cipher_blob_is_version_tagged_and_opaque() -> None:
    cipher = FieldCipher("k")
    blob = cipher.encrypt_str("super-secret-value")
    assert blob.startswith("v1.")
    assert "super-secret-value" not in blob
    assert cipher.is_ciphertext(blob) is True


def test_field_cipher_is_ciphertext_rejects_plaintext_base32() -> None:
    cipher = FieldCipher("k")
    # A raw base32 TOTP secret must NOT look like a ciphertext blob, so the
    # legacy-plaintext tolerance path is unambiguous.
    assert cipher.is_ciphertext("JBSWY3DPEHPK3PXP") is False
    assert cipher.is_ciphertext("") is False


def test_field_cipher_wrong_key_fails_closed() -> None:
    blob = FieldCipher("key-a").encrypt_str("secret")
    with pytest.raises(ValueError, match="could not be decrypted"):
        FieldCipher("key-b").decrypt_str(blob)


def test_config_cipher_is_field_cipher_alias() -> None:
    assert ConfigCipher is FieldCipher


def test_field_cipher_key_rotation_decrypts_old_and_writes_new() -> None:
    # SEC-063 rotation (FIX C): set the new key primary + the old key as a
    # predecessor; old blobs still decrypt, new writes use the new key.
    old = FieldCipher("old-field-key")
    old_blob = old.encrypt_str("totp-secret")
    rotated = FieldCipher("new-field-key", extra_key_materials=["old-field-key"])
    assert rotated.decrypt_str(old_blob) == "totp-secret"  # old row still readable
    new_blob = rotated.encrypt_str("totp-secret")
    # A cipher without the old key can read the NEW blob but not the OLD one.
    new_only = FieldCipher("new-field-key")
    assert new_only.decrypt_str(new_blob) == "totp-secret"
    with pytest.raises(ValueError, match="no accepted key matched"):
        new_only.decrypt_str(old_blob)


def test_verify_hmac_sha256_constant_time_forms() -> None:
    import hashlib
    import hmac

    body = b"payload-bytes"
    good = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_hmac_sha256("s3cret", body, good)
    assert verify_hmac_sha256("s3cret", body, f"sha256={good}")
    assert not verify_hmac_sha256("s3cret", body, "deadbeef")
    assert not verify_hmac_sha256("", body, good)


# --- HmacEnvelopeSigner (SEC-020) --------------------------------------------


def test_envelope_signer_roundtrip_and_rejects_tamper() -> None:
    signer = HmacEnvelopeSigner("bus-key")
    sig = signer.sign("the-envelope")
    assert signer.verify("the-envelope", sig)
    assert not signer.verify("the-envelope", "0" * 64)
    assert not signer.verify("a-different-envelope", sig)
    assert not signer.verify("the-envelope", "")


def test_envelope_signer_empty_key_is_deterministic_dev_key() -> None:
    # Empty must be a FIXED dev key (not per-process ephemeral) so the API and
    # worker verify each other's messages in local dev.
    a, b = HmacEnvelopeSigner(""), HmacEnvelopeSigner("")
    assert b.verify("m", a.sign("m"))


def test_envelope_signer_accepts_rotation_predecessor_key() -> None:
    old = HmacEnvelopeSigner("old-key")
    rotated = HmacEnvelopeSigner("new-key", extra_key_materials=["old-key"])
    # The rotated signer verifies a signature made with the old key (drain window)
    assert rotated.verify("m", old.sign("m"))
    # …but signs with the new key, which the old signer does not accept.
    assert not old.verify("m", rotated.sign("m"))


# --- age v1 (X25519) driver format (ADR-SEC-03) ------------------------------


def test_age_keygen_and_bech32_roundtrip() -> None:
    identity, recipient = age.generate_identity()
    assert identity.startswith("AGE-SECRET-KEY-1")
    assert recipient.startswith("age1")
    sk = age.parse_identity(identity)
    assert age.recipient_str(sk) == recipient
    assert age.identity_str(sk) == identity


@pytest.mark.parametrize("size", [0, 1, 100, 65535, 65536, 65537, 200000])
def test_age_encrypt_decrypt_roundtrip_all_sizes(size: int) -> None:
    identity, recipient = age.generate_identity()
    sk, pk = age.parse_identity(identity), age.parse_recipient(recipient)
    plaintext = bytes((i * 7) % 256 for i in range(size))
    ciphertext = age.encrypt(plaintext, pk)
    assert ciphertext.startswith(b"age-encryption.org/v1\n-> X25519 ")
    assert age.decrypt(ciphertext, sk) == plaintext


def test_age_wrong_identity_fails() -> None:
    _, recipient = age.generate_identity()
    other, _ = age.generate_identity()
    blob = age.encrypt(b"secret", age.parse_recipient(recipient))
    with pytest.raises(age.AgeError):
        age.decrypt(blob, age.parse_identity(other))


def test_age_tampered_payload_and_header_fail() -> None:
    identity, recipient = age.generate_identity()
    sk = age.parse_identity(identity)
    blob = bytearray(age.encrypt(b"hello world", age.parse_recipient(recipient)))
    payload_tamper = bytearray(blob)
    payload_tamper[-1] ^= 0x01
    with pytest.raises(age.AgeError):
        age.decrypt(bytes(payload_tamper), sk)
    header_tamper = bytearray(blob)
    header_tamper[bytes(blob).index(b"\n---") - 5] ^= 0x01
    with pytest.raises(age.AgeError):
        age.decrypt(bytes(header_tamper), sk)
