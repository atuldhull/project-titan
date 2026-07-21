"""The shared redaction engine (TITAN-SEC-054): three detectors, one pipeline.

Every string passes three detectors — live-inventory hashed lookup, provider
key patterns, high-entropy tokens — and every match is replaced with
``[REDACTED:<class>:<hash8>]``, where ``hash8`` is the first 8 hex chars of
the SHA-256 of the matched text. The hash is stable across sinks, so "the
same secret leaked in a gateway payload AND a log line" is correlatable in
the audit trail without revealing anything (TITAN-SEC-092).

The engine never holds plaintext inventory values: the vault hashes each live
secret into an :class:`InventoryIndex` (SHA-256 digests + length bounds), and
the engine hashes candidate tokens out of the text to test membership — a
memory dump of a process running the redactor yields digests, not secrets.

Overlapping matches are MERGED before replacement (fail closed): if a pattern
covers ``[0, 20)`` and a token match covers ``[10, 30)``, replacing only one
would leave a secret fragment in the output, so the union is replaced and the
highest-priority match (inventory > pattern > entropy) names the class.
"""

import contextlib
import hashlib
import re
from collections.abc import Callable, Iterable, Sequence, Set
from dataclasses import dataclass
from typing import Any

from titan.redaction.entropy import is_high_entropy
from titan.redaction.patterns import DEFAULT_PATTERNS

# Inventory values shorter than this are never indexed: redacting every
# occurrence of a trivial string (someone vaults the value "true") would
# destroy logs and prompts platform-wide. Short secrets remain the pattern
# detector's job; the vault dashboard is the place to flag weak values.
MIN_INVENTORY_VALUE_LENGTH = 8

# Candidate tokens for the ENTROPY detector: runs over the usual secret alphabet
# (base64/base64url/hex, dashes, underscores, slashes) with up to two trailing
# '=' of base64 padding. Kept narrow so entropy scoring stays precise (a
# punctuated run would dilute the bits/char).
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")

# Candidate tokens for the INVENTORY detector: a vaulted value can legally
# contain '.', ':', '@', '!', etc. (DB passwords, `id:secret`, SendGrid
# `SG.<id>.<key>`), so tokenizing on the narrow alphabet above would SPLIT the
# secret into fragments that never match the whole-value digest — a silent
# fail-open (Phase-69 security review). This broader run breaks only on
# STRUCTURAL separators (whitespace, quotes, '=', ',', ';', brackets, pipes,
# backslash), so `KEY=value` still isolates `value` while `SG.a.b` stays whole.
_INVENTORY_TOKEN_RE = re.compile(r"""[^\s"'`=,;<>{}()\[\]|\\]{8,}""")

# Punctuation trimmed from an inventory candidate before the digest test, so a
# value trailed by sentence/structural punctuation (`...key is Str0ng.P@ss.`)
# still matches its vaulted form. The WHOLE token span is still redacted.
_TRIM = "\"'.,:;!?()[]{}<>`"


def hash8(text: str) -> str:
    """The stable 8-hex-char identifier stamped into replacements and audits."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Hit:
    """One redaction occurrence: the detector class and the match's hash8."""

    cls: str
    hash8: str


class InventoryIndex:
    """Hashed lookup over the live secret inventory. Immutable — refreshes swap
    a whole new index onto the engine, so readers never see a partial set."""

    __slots__ = ("_digests", "_max_len", "_min_len")

    def __init__(self, digests: Set[str], *, min_len: int = 0, max_len: int = 0) -> None:
        self._digests = frozenset(digests)
        self._min_len = min_len
        self._max_len = max_len

    def __len__(self) -> int:
        return len(self._digests)

    def match(self, token: str) -> str | None:
        """The canonical matched value if *token* — or its base64-padding- or
        punctuation-trimmed variant — is a live secret, else None."""
        if not self._digests:
            return None
        seen: set[str] = set()
        for candidate in (token, token.rstrip("="), token.strip(_TRIM)):
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if (
                self._min_len <= len(candidate) <= self._max_len
                and _digest(candidate) in self._digests
            ):
                return candidate
        return None

    @classmethod
    def from_values(cls, values: Iterable[str]) -> "InventoryIndex":
        """Hash live secret VALUES into an index. Values shorter than
        MIN_INVENTORY_VALUE_LENGTH are skipped; whitespace- and base64-padding
        variants are indexed so tokenized text still matches exactly."""
        digests: set[str] = set()
        min_len = 0
        max_len = 0
        for value in values:
            for variant in {value, value.strip(), value.rstrip("=")}:
                if len(variant) < MIN_INVENTORY_VALUE_LENGTH:
                    continue
                digests.add(_digest(variant))
                min_len = len(variant) if min_len == 0 else min(min_len, len(variant))
                max_len = max(max_len, len(variant))
        return cls(digests, min_len=min_len, max_len=max_len)


EMPTY_INVENTORY = InventoryIndex(frozenset())

Reporter = Callable[[str, Sequence[Hit]], None]

# Sinks whose output crosses TITAN's trust boundary to a third party: the
# marker there carries NO hash8. hash8 is an internal cross-sink correlator
# (Loki/audit); handing a stable, unsalted 32-bit fingerprint of a secret to
# an LLM provider is a needless info-leak + confirmation oracle (Phase-69
# security review). The class stays (the provider already infers "a key was
# here"); the Hit still carries hash8 for the internal audit trail.
_NO_CORRELATOR_SINKS = frozenset({"gateway"})

# start, end, priority (inventory=0 < pattern=1 < entropy=2), tie-break order,
# class, hash8 — priority names the class when overlapping spans merge.
_Span = tuple[int, int, int, int, str, str]


class RedactionEngine:
    """One engine instance serves every sink. ``redact`` is synchronous and
    lock-free on the hot path (log processor, stream chunks); the inventory
    and reporter are swapped atomically by reference assignment."""

    def __init__(
        self,
        *,
        patterns: Sequence[tuple[str, re.Pattern[str]]] | None = None,
        inventory: InventoryIndex | None = None,
    ) -> None:
        self._patterns = tuple(DEFAULT_PATTERNS if patterns is None else patterns)
        self._inventory = EMPTY_INVENTORY if inventory is None else inventory
        self._reporter: Reporter | None = None

    @property
    def inventory_size(self) -> int:
        return len(self._inventory)

    def set_inventory(self, index: InventoryIndex) -> None:
        self._inventory = index

    def set_reporter(self, reporter: Reporter | None) -> None:
        self._reporter = reporter

    def redact(self, text: str, *, sink: str = "unknown") -> str:
        redacted, hits = self.redact_with_hits(text, sink=sink)
        if hits and self._reporter is not None:
            # A broken reporter must never block redaction (or recurse through
            # the log sink) — the redacted text is the contract, the audit is
            # best-effort.
            with contextlib.suppress(Exception):
                self._reporter(sink, hits)
        return redacted

    def redact_with_hits(self, text: str, *, sink: str = "unknown") -> tuple[str, list[Hit]]:
        if not text:
            return text, []
        spans = self._collect(text)
        if not spans:
            return text, []
        correlate = sink not in _NO_CORRELATOR_SINKS
        pieces: list[str] = []
        hits: list[Hit] = []
        cursor = 0
        for start, end, cls, digest in _merge(spans):
            pieces.append(text[cursor:start])
            pieces.append(f"[REDACTED:{cls}:{digest}]" if correlate else f"[REDACTED:{cls}]")
            hits.append(Hit(cls=cls, hash8=digest))
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces), hits

    def _collect(self, text: str) -> list[_Span]:
        spans: list[_Span] = []
        for order, (cls, rx) in enumerate(self._patterns):
            for m in rx.finditer(text):
                spans.append((m.start(), m.end(), 1, order, cls, hash8(m.group())))
        inventory = self._inventory
        if len(inventory) > 0:
            # Inventory over the BROAD tokenizer so punctuated vaulted values
            # (with '.', ':', '@', …) are tested whole, not in fragments. The
            # hash8 is of the matched VALUE (for correlation) but the WHOLE
            # token span is redacted so no fragment survives.
            for m in _INVENTORY_TOKEN_RE.finditer(text):
                matched = inventory.match(m.group())
                if matched is not None:
                    spans.append((m.start(), m.end(), 0, -1, "inventory", hash8(matched)))
        # Entropy over the NARROW tokenizer so scoring stays precise.
        for m in _TOKEN_RE.finditer(text):
            token = m.group()
            if is_high_entropy(token):
                spans.append((m.start(), m.end(), 2, 10_000, "entropy", hash8(token)))
        return spans


def _merge(spans: list[_Span]) -> list[tuple[int, int, str, str]]:
    """Coalesce overlapping spans into their union; the highest-priority
    (then longest, then earliest-listed) member names the merged replacement."""
    spans.sort(key=lambda s: (s[0], s[1]))
    merged: list[tuple[int, int, str, str]] = []
    group: list[_Span] = []
    group_end = -1
    for span in spans:
        if group and span[0] >= group_end:
            merged.append(_primary(group, group_end))
            group = []
        group.append(span)
        group_end = max(group_end, span[1])
    if group:
        merged.append(_primary(group, group_end))
    return merged


def _primary(group: list[_Span], end: int) -> tuple[int, int, str, str]:
    start = min(s[0] for s in group)
    best = min(group, key=lambda s: (s[2], -(s[1] - s[0]), s[3]))
    return (start, end, best[4], best[5])


def redact_structure(value: Any, *, engine: RedactionEngine, sink: str) -> Any:
    """Recursively redact every string in a JSON-ish structure — keys included,
    because tool results can key mappings by data-derived strings. Returns a
    new structure; never mutates the input."""
    if isinstance(value, str):
        return engine.redact(value, sink=sink)
    if isinstance(value, dict):
        return {
            (engine.redact(k, sink=sink) if isinstance(k, str) else k): redact_structure(
                v, engine=engine, sink=sink
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item, engine=engine, sink=sink) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structure(item, engine=engine, sink=sink) for item in value)
    return value


__all__ = [
    "EMPTY_INVENTORY",
    "MIN_INVENTORY_VALUE_LENGTH",
    "Hit",
    "InventoryIndex",
    "RedactionEngine",
    "Reporter",
    "hash8",
    "redact_structure",
]
