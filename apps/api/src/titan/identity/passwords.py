"""Password hashing — Argon2id only (TITAN-SEC-017). Private to identity.

Three hardening changes landed in Phase 67, each fixing a way the Phase-8
version was quietly unsafe rather than merely minimal:

- **`InvalidHashError` is caught.** `verify()` raises it for a hash that is
  malformed or in a format this hasher does not recognise. Phase 8 caught only
  `VerifyMismatchError`/`VerificationError`, so a corrupt row — or a legacy
  hash from a future migration — was an unhandled 500 at the login endpoint,
  not a 401. A verify that cannot read the stored hash is a failed verify.
- **`needs_rehash` is exposed.** Argon2's parameters (time/memory/parallelism)
  are meant to rise as hardware improves, and the stored PHC string carries the
  parameters it was made with. Without a rehash check a parameter bump would
  reach only brand-new passwords, never anyone who already had one. The verify
  path calls this on success and re-stores when it returns True.
- **A dummy hash is published for the timing-oracle fix.** The caller
  (`login_local`) must run a verify even when the account does not exist, or an
  unknown email returns in ~0 ms against the ~50-100 ms a real Argon2 verify
  costs — a user-enumeration oracle. `DUMMY_HASH` is a real Argon2id hash of a
  fixed string, so verifying against it costs the same as a real miss.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id by default

# A valid Argon2id hash to compare against when there is no real credential, so
# the enumeration path spends the same time as a genuine verify. Computed once
# at import with the same hasher, so it tracks the current parameters.
DUMMY_HASH = _hasher.hash("titan-timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    """True iff `candidate` matches. Never raises on a bad or unreadable hash."""
    try:
        return _hasher.verify(password_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether this hash was made with weaker parameters than current policy.

    Returns False for a hash this hasher cannot parse: a malformed hash is for
    the verify path to reject, not something to silently "upgrade".
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False
