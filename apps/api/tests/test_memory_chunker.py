"""The chunking interface — token counting, the contextual header, header
assembly, the shared windowing primitive, and the fixed-size fallback + registry
(Vol 5 §5.2). Pure unit tests, no DB. The four content-aware chunkers have their
own golden suite in test_memory_chunkers_golden.py."""

from __future__ import annotations

import subprocess
import sys

import pytest

from titan.memory.chunker import (
    Chunk,
    ChunkContext,
    FixedSizeChunker,
    assemble,
    contextual_header,
    count_tokens,
    get_chunker,
    register_chunker,
    tail_tokens,
    window_by_tokens,
)
from titan.memory.chunkers import (
    CodeChunker,
    ConversationChunker,
    MarkdownChunker,
    TableChunker,
)

CTX = ChunkContext(scope="org", kind="semantic", title="A Note")


def _body(chunk: Chunk) -> str:
    """The chunk body — everything after the header line."""
    return chunk.text.split("\n", 1)[1]


# --- token counting / windowing primitives ---------------------------------


def test_count_tokens_counts_words_and_punctuation() -> None:
    # A token is a word OR a single punctuation mark, so punctuation-dense code
    # counts closer to a real subword tokenizer than whitespace splitting would.
    assert count_tokens("one two three") == 3
    assert count_tokens("a = b + c;") == 6  # a = b + c ;
    assert count_tokens("") == 0


def test_window_by_tokens_overlap_is_contiguous_and_original() -> None:
    assert window_by_tokens("a b c d e", 3, 1) == ["a b c", "c d e"]  # 1-token overlap
    assert window_by_tokens("one two three four five", 2, 0) == ["one two", "three four", "five"]
    assert window_by_tokens("", 4, 0) == []  # no tokens -> no windows


def test_window_by_tokens_preserves_original_substring() -> None:
    body = "line one\n  indented two\tthree"
    assert window_by_tokens(body, 100, 0) == [body]


def test_window_by_tokens_last_window_not_re_emitted() -> None:
    assert window_by_tokens("w x y z", 3, 2) == ["w x y", "x y z"]


def test_window_by_tokens_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        window_by_tokens("a b c", 0, 0)
    with pytest.raises(ValueError):
        window_by_tokens("a b c", 3, 3)


def test_tail_tokens_returns_trailing_span_or_empty() -> None:
    assert tail_tokens("alpha beta gamma delta", 2) == "gamma delta"
    assert tail_tokens("alpha beta", 0) == ""
    assert tail_tokens("   ", 3) == ""


# --- contextual header + assembly ------------------------------------------


def test_contextual_header_exact_format() -> None:
    # The §5.2 header is exact: [scope | kind | title | chunk seq/n].
    assert contextual_header(CTX, 0, 3) == "[org | semantic | A Note | chunk 0/3]"


def test_assemble_prefixes_header_numbers_seq_and_counts_full_text() -> None:
    chunks = assemble(["first body", "second body here"], CTX)
    assert [c.seq for c in chunks] == [0, 1]
    assert chunks[0].text == "[org | semantic | A Note | chunk 0/2]\nfirst body"
    assert [_body(c) for c in chunks] == ["first body", "second body here"]
    # token_count is over the FULL stored text (header + body) — what gets embedded.
    assert chunks[0].token_count == count_tokens(chunks[0].text)
    assert chunks[0].token_count > count_tokens("first body")


def test_assemble_empty_yields_one_header_only_chunk() -> None:
    chunks = assemble([], CTX)
    assert len(chunks) == 1
    assert chunks[0].seq == 0
    assert chunks[0].text == "[org | semantic | A Note | chunk 0/1]\n"


# --- fixed-size fallback ----------------------------------------------------


def test_fixed_size_fallback_windows_with_header() -> None:
    chunks = FixedSizeChunker(window_tokens=3, overlap_tokens=1).chunk("a b c d e", CTX)
    assert [_body(c) for c in chunks] == ["a b c", "c d e"]
    assert [c.seq for c in chunks] == [0, 1]
    assert chunks[0].text.startswith("[org | semantic | A Note | chunk 0/2]\n")


def test_fixed_size_fallback_empty_body_is_one_chunk() -> None:
    for body in ("", "   \n\t "):
        chunks = FixedSizeChunker().chunk(body, CTX)
        assert len(chunks) == 1 and chunks[0].seq == 0 and _body(chunks[0]) == ""


def test_fixed_size_fallback_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        FixedSizeChunker(window_tokens=0)
    with pytest.raises(ValueError):
        FixedSizeChunker(window_tokens=4, overlap_tokens=4)


# --- registry ---------------------------------------------------------------


def test_registry_binds_content_aware_chunkers_and_falls_back() -> None:
    # Phase 34 registers a content-aware chunker per §5.2 type; unknown types
    # still fall back to fixed-size windows.
    assert isinstance(get_chunker("markdown"), MarkdownChunker)
    assert isinstance(get_chunker("prose"), MarkdownChunker)
    assert isinstance(get_chunker("code"), CodeChunker)
    assert isinstance(get_chunker("conversation"), ConversationChunker)
    assert isinstance(get_chunker("table"), TableChunker)
    assert isinstance(get_chunker("totally-unknown"), FixedSizeChunker)


def test_registry_accepts_overrides_without_leaking() -> None:
    class _Sentinel:
        def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
            return [Chunk(seq=0, text=text, token_count=1)]

    sentinel = _Sentinel()
    original = get_chunker("code")
    register_chunker("code", sentinel)
    try:
        assert get_chunker("code") is sentinel
        assert isinstance(get_chunker("markdown"), MarkdownChunker)  # others untouched
    finally:
        register_chunker("code", original)  # restore so state does not leak


@pytest.mark.parametrize(
    "first_import",
    ["titan.memory.chunkers", "titan.memory.chunker", "titan.memory.service"],
)
def test_registration_survives_any_import_order(first_import: str) -> None:
    """The chunker/chunkers import cycle must populate the registry regardless of
    which module is imported first (a fresh interpreter each time, since in-process
    the modules are already loaded). Guards the self-registration wiring."""
    code = (
        f"import {first_import}; "
        "from titan.memory.chunker import get_chunker; "
        "from titan.memory.chunkers import MarkdownChunker, CodeChunker; "
        "assert isinstance(get_chunker('markdown'), MarkdownChunker); "
        "assert isinstance(get_chunker('code'), CodeChunker); "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_production_chunkers_use_the_5_2_matrix_targets() -> None:
    # §5.2 matrix: markdown 512/64, code 400/0, conversation 512/1turn, table 384.
    md = get_chunker("markdown")
    assert isinstance(md, MarkdownChunker)
    assert (md._target, md._overlap) == (512, 64)
    code = get_chunker("code")
    assert isinstance(code, CodeChunker)
    assert code._target == 400
    conv = get_chunker("conversation")
    assert isinstance(conv, ConversationChunker)
    assert conv._target == 512
    table = get_chunker("table")
    assert isinstance(table, TableChunker)
    assert table._target == 384
