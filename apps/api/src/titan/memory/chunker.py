"""Chunking (Vol 5 §5.2) — the interface + the trivial fixed-size fallback.

The writer declares a ``content_type`` on each Memory Event; chunking is keyed on
it. Phase 33 ships ONE implementation — a fixed-size flat-token-window fallback —
behind a stable :class:`Chunker` interface and a registry, so the four
content-aware chunkers of §5.2 (heading-aware markdown, tree-sitter code,
turn-grouped conversation, row-grouped tables) drop in next phase (Phase 34) by
registering against their content types, with zero change to the write path.

"Token" here is a whitespace-delimited run — a deliberately cheap approximation
for the fallback; the real chunkers use the embedding model's tokenizer. Chunk
text preserves the ORIGINAL substring (whitespace and newlines intact), never a
re-joined lossy copy, so downstream embedding sees faithful content.
"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

# §5.2 content types (the writer declares one per event). Phase 33 maps every
# type to the fixed-size fallback; Phase 34 registers content-aware chunkers.
ContentType = Literal["markdown", "prose", "code", "conversation", "table"]
CONTENT_TYPES: tuple[str, ...] = ("markdown", "prose", "code", "conversation", "table")

# §5.2 markdown/prose defaults — a sensible fallback window for all types until
# the content-aware chunkers land.
DEFAULT_WINDOW_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64

_TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class Chunk:
    """One chunk of a record's body: its position, text, and token count."""

    seq: int
    text: str
    token_count: int


class Chunker(Protocol):
    """Splits a record body into ordered, token-bounded chunks. Every registered
    content-aware splitter (§5.2) implements this."""

    def chunk(self, text: str) -> list[Chunk]: ...


class FixedSizeChunker:
    """Flat fixed-size token windows with overlap — the content-agnostic fallback.

    Slides a ``window_tokens`` window across the whitespace-token stream, stepping
    by ``window_tokens - overlap_tokens``. Each chunk's text is the original
    substring spanning its tokens, so nothing is reflowed or lost."""

    def __init__(
        self,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        if window_tokens < 1:
            raise ValueError("window_tokens must be >= 1")
        if not 0 <= overlap_tokens < window_tokens:
            raise ValueError("overlap_tokens must be in [0, window_tokens)")
        self._window = window_tokens
        self._overlap = overlap_tokens

    def chunk(self, text: str) -> list[Chunk]:
        spans = [(m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
        if not spans:
            # No tokens (empty/whitespace body) — one empty-content chunk keeps the
            # record embeddable-shaped without inventing text.
            return [Chunk(seq=0, text=text, token_count=0)]
        step = self._window - self._overlap
        chunks: list[Chunk] = []
        start = 0
        seq = 0
        while start < len(spans):
            end = min(start + self._window, len(spans))
            chunk_text = text[spans[start][0] : spans[end - 1][1]]
            chunks.append(Chunk(seq=seq, text=chunk_text, token_count=end - start))
            seq += 1
            if end == len(spans):
                break  # the last window reached the end; overlap must not re-emit it
            start += step
        return chunks


# --- registry (content_type -> Chunker) ------------------------------------

_FALLBACK: Chunker = FixedSizeChunker()
_REGISTRY: dict[str, Chunker] = {}


def register_chunker(content_type: str, chunker: Chunker) -> None:
    """Register (or override) the chunker for a content type — the seam Phase 34's
    content-aware splitters bind to."""
    _REGISTRY[content_type] = chunker


def get_chunker(content_type: str) -> Chunker:
    """The chunker for ``content_type``; the fixed-size fallback until a
    content-aware one is registered (§5.2)."""
    return _REGISTRY.get(content_type, _FALLBACK)


__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_WINDOW_TOKENS",
    "Chunk",
    "Chunker",
    "ContentType",
    "FixedSizeChunker",
    "get_chunker",
    "register_chunker",
]
