"""Chunking (Vol 5 §5.2) — the interface, shared header assembly, and the
fixed-size fallback.

The writer declares a ``content_type`` on each Memory Event; chunking is keyed on
it. Phase 34 ships the four content-aware splitters (heading-aware markdown,
tree-sitter code, turn-grouped conversation, row-grouped tables) in
:mod:`titan.memory.chunkers`, registered against their content types; the
fixed-size fallback here remains only for unknown types.

Every chunk carries a contextual header prefix (§5.2) —
``[{scope} | {kind} | {title} | chunk {seq}/{n}]`` — prepended to the body so an
isolated chunk stays interpretable to the embedder and reranker. Header assembly
lives in :func:`assemble` so the exact format is defined once, for every chunker.

"Token" here is a word or a single punctuation mark (``\\w+|[^\\w\\s]``) — a
deliberately cheap, deterministic approximation that tracks a real subword
tokenizer far better than whitespace splitting on punctuation-dense code and
tables, so the §5.2 budgets behave sensibly. The real embedding tokenizer arrives
with the provider in a later phase (TITAN-BRN-006); :func:`count_tokens` (and the
single :data:`_TOKEN_RE` it shares with the window/overlap primitives) is the seam
to swap. Chunk bodies preserve the ORIGINAL substring (whitespace and newlines
intact), never a re-joined lossy copy, so downstream embedding sees faithful
content.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# §5.2 content types (the writer declares one per event). Phase 34 registers a
# content-aware chunker for each; unknown types fall back to fixed-size windows.
ContentType = Literal["markdown", "prose", "code", "conversation", "table"]
CONTENT_TYPES: tuple[str, ...] = ("markdown", "prose", "code", "conversation", "table")

# §5.2 markdown/prose defaults — also the fallback window for unknown types.
DEFAULT_WINDOW_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64

# One token = a word or a single punctuation mark. The single definition shared
# by counting, windowing, and overlap so every part of chunking measures the same
# unit (a "512-token window" and a "512-token count" mean the same thing).
# Swapping in the real embedding tokenizer (TITAN-BRN-006) happens HERE — at the
# module seam — never per chunker instance, or window/seed sizing and budget
# arithmetic would silently measure in different units.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")

# One deliberate divergence between WINDOWING and COST (Phase 38): a ``\w+``
# run longer than this is not one linguistic token — hex dumps, base64,
# minified blobs. Real BPE bills them at ~len/4, and counting them as 1 would
# let a single 400k-char run bypass every token budget in the system
# (TITAN-BRN-015: packet fill, prompt slots, tool-result clamps). Ordinary
# words and code identifiers stay under the threshold, so chunk boundaries,
# stored token counts, and eval baselines for real text are UNCHANGED;
# windowing still slices by span so chunk shapes never depend on run length.
LONG_RUN_CHARS = 48
_LONG_RUN_DIVISOR = 4


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def token_cost(start: int, end: int) -> int:
    """The billed cost of one token span: 1, or ~len/4 for abnormal runs."""
    length = end - start
    if length <= LONG_RUN_CHARS:
        return 1
    return -(-length // _LONG_RUN_DIVISOR)  # ceil(length / 4)


def count_tokens(text: str) -> int:
    """Deterministic token estimate (§5.2): one per word/punctuation span, with
    abnormally long unbroken runs billed at ~len/4 (the BPE reality)."""
    return sum(token_cost(start, end) for start, end in _token_spans(text))


def tail_tokens(text: str, n: int) -> str:
    """The original substring spanning the last ``n`` tokens of ``text`` — the
    overlap seed. ``""`` when ``n<=0`` or ``text`` has no tokens."""
    if n <= 0:
        return ""
    spans = _token_spans(text)
    if not spans:
        return ""
    take = spans[-n:]
    return text[take[0][0] : take[-1][1]]


@dataclass(frozen=True)
class Chunk:
    """One chunk of a record's body: its position, stored text (header + body),
    and token count of that stored text."""

    seq: int
    text: str
    token_count: int


@dataclass(frozen=True)
class ChunkContext:
    """Record-level metadata a chunker needs to build the §5.2 contextual header.
    Supplied by the write path from the record being chunked; ``title`` and the
    scope/kind are used verbatim (the format is fixed, not escaped)."""

    scope: str
    kind: str
    title: str


class Chunker(Protocol):
    """Splits a record body into ordered, header-prefixed, token-bounded chunks.
    Every registered content-aware splitter (§5.2) implements this. ``ctx`` is the
    record metadata the contextual header is built from."""

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]: ...


def contextual_header(ctx: ChunkContext, seq: int, n: int) -> str:
    """The §5.2 contextual header prefix, exactly
    ``[{scope} | {kind} | {title} | chunk {seq}/{n}]`` — ``seq`` is the stored
    chunk's 0-based position (matching ``memory_chunks.seq``), ``n`` the total."""
    return f"[{ctx.scope} | {ctx.kind} | {ctx.title} | chunk {seq}/{n}]"


def assemble(bodies: Sequence[str], ctx: ChunkContext) -> list[Chunk]:
    """Turn ordered body segments into stored :class:`Chunk` objects: prepend the
    contextual header to each, number them ``seq`` from 0, and count tokens on the
    FULL stored text (header + body) — what actually gets embedded. ``n`` (the
    total) is known only after splitting, so header assembly happens here, once,
    for every chunker. An empty body still yields one header-only chunk so the
    record stays embeddable-shaped."""
    segments = list(bodies) if bodies else [""]
    n = len(segments)
    chunks: list[Chunk] = []
    for seq, body in enumerate(segments):
        header = contextual_header(ctx, seq, n)
        text = f"{header}\n{body}"
        chunks.append(Chunk(seq=seq, text=text, token_count=count_tokens(text)))
    return chunks


def window_by_tokens(text: str, window: int, overlap: int) -> list[str]:
    """Slide a ``window``-token window across the token stream, stepping by
    ``window - overlap``; each segment is the ORIGINAL substring spanning its
    tokens (nothing reflowed). The shared primitive behind the fixed-size fallback
    and the last-resort hard split of an oversize atomic unit. Returns ``[]`` for a
    body with no tokens (the caller decides how to represent an empty body)."""
    if window < 1:
        raise ValueError("window must be >= 1")
    if not 0 <= overlap < window:
        raise ValueError("overlap must be in [0, window)")
    spans = _token_spans(text)
    if not spans:
        return []
    step = window - overlap
    out: list[str] = []
    start = 0
    while start < len(spans):
        end = min(start + window, len(spans))
        out.append(text[spans[start][0] : spans[end - 1][1]])
        if end == len(spans):
            break  # last window reached the end; overlap must not re-emit it
        start += step
    return out


class FixedSizeChunker:
    """Flat fixed-size token windows with overlap — the content-agnostic fallback
    for unknown content types. Bodies are original substrings; the header is added
    by :func:`assemble`."""

    def __init__(
        self,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        # Validated eagerly (mirrors window_by_tokens) so a misconfigured chunker
        # fails at construction, not mid-write.
        if window_tokens < 1:
            raise ValueError("window_tokens must be >= 1")
        if not 0 <= overlap_tokens < window_tokens:
            raise ValueError("overlap_tokens must be in [0, window_tokens)")
        self._window = window_tokens
        self._overlap = overlap_tokens

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
        return assemble(window_by_tokens(text, self._window, self._overlap), ctx)


# --- registry (content_type -> Chunker) ------------------------------------

_FALLBACK: Chunker = FixedSizeChunker()
_REGISTRY: dict[str, Chunker] = {}


def register_chunker(content_type: str, chunker: Chunker) -> None:
    """Register (or override) the chunker for a content type — the seam the
    content-aware splitters of :mod:`titan.memory.chunkers` bind to."""
    _REGISTRY[content_type] = chunker


def get_chunker(content_type: str) -> Chunker:
    """The chunker for ``content_type``; the fixed-size fallback for any type
    without a registered content-aware splitter (§5.2)."""
    return _REGISTRY.get(content_type, _FALLBACK)


__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_WINDOW_TOKENS",
    "Chunk",
    "ChunkContext",
    "Chunker",
    "ContentType",
    "FixedSizeChunker",
    "assemble",
    "contextual_header",
    "count_tokens",
    "get_chunker",
    "register_chunker",
    "tail_tokens",
    "window_by_tokens",
]

# Registering the §5.2 content-aware chunkers is a side effect of importing this
# module: any code path that chunks imports `titan.memory.chunker`, so the
# registry is always populated deterministically. `chunkers` self-registers on
# import (its bottom calls register_builtin_chunkers), so this is a pure
# side-effect import — no attribute is touched on the possibly-partial module,
# which keeps it safe under EITHER import order despite the cycle (chunkers.py
# imports from this module at its top). Placed last, after every name above is
# defined, so chunkers.py sees a fully-populated chunker module.
from titan.memory import chunkers as _chunkers  # noqa: E402, F401  (registers §5.2 chunkers)
