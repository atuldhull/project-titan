"""Argon2id verify-path hardening (Phase 67; TITAN-SEC-017).

These cover the three ways the Phase-8 verify path was unsafe: a malformed
stored hash crashing instead of failing, a parameter bump never reaching
existing passwords, and an unknown email answering faster than a real one.
"""

from __future__ import annotations

import time

import pytest
from argon2 import PasswordHasher

from titan.identity import passwords


def test_valid_password_verifies() -> None:
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password(h, "correct horse battery staple") is True


def test_wrong_password_fails() -> None:
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password(h, "Tr0ub4dour") is False


def test_malformed_hash_is_a_failed_verify_not_a_crash() -> None:
    """The 500-vs-401 fix: a corrupt row must not raise out of the login path."""
    assert passwords.verify_password("not-a-real-argon2-hash", "anything") is False
    assert passwords.verify_password("", "anything") is False
    assert passwords.verify_password("$argon2id$broken", "anything") is False


def test_needs_rehash_false_for_a_current_hash() -> None:
    h = passwords.hash_password("current-params")
    assert passwords.needs_rehash(h) is False


def test_needs_rehash_true_for_a_weaker_hash() -> None:
    """A hash made with weaker parameters than the module default is upgradable."""
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    weak_hash = weak.hash("legacy")
    assert passwords.needs_rehash(weak_hash) is True
    # ...and it still verifies, so the upgrade can happen on a real login.
    assert passwords.verify_password(weak_hash, "legacy") is True


def test_needs_rehash_false_for_a_malformed_hash() -> None:
    """A hash we cannot parse is for verify to reject, not for rehash to 'fix'."""
    assert passwords.needs_rehash("garbage") is False


def test_dummy_hash_is_a_real_argon2id_hash() -> None:
    assert passwords.DUMMY_HASH.startswith("$argon2id$")
    # It must not verify the equaliser string via any caller-reachable path —
    # it only exists to be verified AGAINST and always miss real input.
    assert passwords.verify_password(passwords.DUMMY_HASH, "anything else") is False


def test_verifying_against_the_dummy_costs_the_same_as_a_real_miss() -> None:
    """The timing-oracle equaliser: a miss on the dummy ≈ a miss on a real hash.

    This is the property `login_local` relies on. Argon2 is data-independent, so
    two verifies with the same parameters take essentially the same time; assert
    they are within a wide factor rather than a tight bound, since the point is
    that one is not ORDERS of magnitude faster (the ~0 ms short-circuit was).
    """
    real = passwords.hash_password("a real user's password")

    def timed(h: str) -> float:
        start = time.perf_counter()
        for _ in range(3):
            passwords.verify_password(h, "guess that is wrong")
        return time.perf_counter() - start

    # Warm up so import/JIT costs do not skew the first sample.
    timed(real)
    real_ms = timed(real)
    dummy_ms = timed(passwords.DUMMY_HASH)

    ratio = max(real_ms, dummy_ms) / max(min(real_ms, dummy_ms), 1e-6)
    assert ratio < 5.0, f"dummy/real verify timings diverged: {real_ms=} {dummy_ms=}"


@pytest.mark.parametrize("bad", ["", "   ", "$argon2id$", "$2b$12$notargon"])
def test_assorted_bad_hashes_never_raise(bad: str) -> None:
    assert passwords.verify_password(bad, "x") is False
    assert passwords.needs_rehash(bad) is False
