"""The fixed-size fallback chunker + the content-type registry (Vol 5 §5.2,
Phase 33) — pure unit tests, no DB."""

from __future__ import annotations

import pytest

from titan.memory.chunker import (
    Chunk,
    FixedSizeChunker,
    get_chunker,
    register_chunker,
)


def test_windows_with_overlap_are_contiguous_and_counted() -> None:
    chunker = FixedSizeChunker(window_tokens=3, overlap_tokens=1)
    chunks = chunker.chunk("a b c d e")
    assert [c.text for c in chunks] == ["a b c", "c d e"]  # 1-token overlap ("c")
    assert [c.seq for c in chunks] == [0, 1]  # contiguous, zero-based
    assert [c.token_count for c in chunks] == [3, 3]


def test_no_overlap_partitions_exactly() -> None:
    chunker = FixedSizeChunker(window_tokens=2, overlap_tokens=0)
    chunks = chunker.chunk("one two three four five")
    assert [c.text for c in chunks] == ["one two", "three four", "five"]
    assert [c.token_count for c in chunks] == [2, 2, 1]


def test_chunk_text_preserves_original_substring() -> None:
    """Chunk text is the ORIGINAL span (newlines/indentation intact), never a
    lossy re-join — downstream embedding sees faithful content."""
    body = "line one\n  indented two\tthree"
    chunks = FixedSizeChunker(window_tokens=100, overlap_tokens=0).chunk(body)
    assert len(chunks) == 1 and chunks[0].text == body


def test_short_body_is_a_single_chunk() -> None:
    chunks = FixedSizeChunker().chunk("hello world")
    assert chunks == [Chunk(seq=0, text="hello world", token_count=2)]


def test_empty_and_whitespace_bodies_yield_one_empty_chunk() -> None:
    for body in ("", "   \n\t "):
        chunks = FixedSizeChunker().chunk(body)
        assert len(chunks) == 1 and chunks[0].seq == 0 and chunks[0].token_count == 0


def test_last_window_is_not_re_emitted_by_overlap() -> None:
    # 4 tokens, window 3, overlap 2 (step 1): 0-3, 1-4 — the second reaches the
    # end and stops; no degenerate trailing duplicate.
    chunks = FixedSizeChunker(window_tokens=3, overlap_tokens=2).chunk("w x y z")
    assert [c.text for c in chunks] == ["w x y", "x y z"]


def test_invalid_window_or_overlap_rejected() -> None:
    with pytest.raises(ValueError):
        FixedSizeChunker(window_tokens=0)
    with pytest.raises(ValueError):
        FixedSizeChunker(window_tokens=4, overlap_tokens=4)  # overlap must be < window


def test_registry_defaults_to_fallback_and_accepts_overrides() -> None:
    # Every content type falls back to the fixed-size chunker until one is
    # registered (Phase 34 registers the content-aware splitters here).
    assert isinstance(get_chunker("markdown"), FixedSizeChunker)
    assert isinstance(get_chunker("code"), FixedSizeChunker)
    assert isinstance(get_chunker("totally-unknown"), FixedSizeChunker)

    class _Sentinel:
        def chunk(self, text: str) -> list[Chunk]:
            return [Chunk(seq=0, text=text, token_count=1)]

    sentinel = _Sentinel()
    register_chunker("code", sentinel)
    try:
        assert get_chunker("code") is sentinel
        assert isinstance(get_chunker("markdown"), FixedSizeChunker)  # others untouched
    finally:
        # Restore the fallback so registry state does not leak across tests.
        from titan.memory import chunker as chunker_mod

        chunker_mod._REGISTRY.pop("code", None)
