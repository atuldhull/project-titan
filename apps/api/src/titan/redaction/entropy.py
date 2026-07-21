"""High-entropy token detection (TITAN-SEC-054 detector 2).

Unanchored entropy detection over-fires catastrophically without guards — the
identifiers that flood TITAN's own logs and prompts (UUIDv7 request ids,
32-hex trace ids, SHA digests, file paths, long camelCase names) all look
"random-ish". The guards here are the difference between a usable log pipeline
and one where every trace id reads ``[REDACTED:entropy:…]``:

- length < 24 never fires (short secrets are the inventory/pattern detectors'
  job — unanchored short-token entropy is pure noise);
- UUIDs and pure-hex strings never fire (request/trace/span ids, digests; a
  hex-encoded secret is caught by the inventory detector when vaulted);
- SRI prefixes (``sha256-…`` lockfile integrity hashes) never fire;
- a token with no digit never fires (random secrets essentially always carry
  digits; long English identifiers essentially never trip 4.0 bits/char, and
  this guard removes the residual tail);
- path-like tokens (2+ ``/``) need 4.5 bits/char instead of 4.0 — repo paths
  with digits can brush 4.0, real base64 sits well above 4.5.

Direction of error is deliberate: over-redaction is an inconvenience,
under-redaction is an incident.
"""

import math
import re
from collections import Counter

MIN_TOKEN_LENGTH = 24
ENTROPY_THRESHOLD = 4.0
PATHLIKE_ENTROPY_THRESHOLD = 4.5

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_HEX_RE = re.compile(r"[0-9a-fA-F]+\Z")
_DIGIT_RE = re.compile(r"[0-9]")
_SRI_PREFIXES = ("sha256-", "sha384-", "sha512-")


def shannon_entropy(text: str) -> float:
    """Bits per character over the sample's own alphabet."""
    if not text:
        return 0.0
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in Counter(text).values())


def is_high_entropy(token: str) -> bool:
    if len(token) < MIN_TOKEN_LENGTH:
        return False
    if token.startswith(_SRI_PREFIXES):
        return False
    core = token.rstrip("=")
    if _UUID_RE.match(core) or _HEX_RE.match(core):
        return False
    if _DIGIT_RE.search(core) is None:
        return False
    threshold = PATHLIKE_ENTROPY_THRESHOLD if core.count("/") >= 2 else ENTROPY_THRESHOLD
    return shannon_entropy(core) >= threshold


__all__ = ["ENTROPY_THRESHOLD", "MIN_TOKEN_LENGTH", "is_high_entropy", "shannon_entropy"]
