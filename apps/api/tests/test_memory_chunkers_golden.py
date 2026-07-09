"""Golden-file + invariant tests for the four content-aware chunkers
(Vol 5 §5.2, TITAN-BRN-008).

Two layers of assurance:

  * **Golden files** — each representative fixture (a real Markdown doc, Python /
    TypeScript / SQL sources, a multi-turn transcript, a Markdown table, a DDL
    dump) is chunked by its PRODUCTION-registered chunker (the §5.2 matrix
    targets), and the exact result is checked in as ``<fixture>.golden.json``. Any
    boundary drift shows up as a visible diff. Regenerate with
    ``uv run python tests/test_memory_chunkers_golden.py`` after an intended change.

  * **Invariants** — properties that must hold for ANY input at ANY target:
    body sizes within budget, the §5.2 overlaps (64-token for markdown, 1-turn
    for conversation), repeated table headers, and — the core of TITAN-BRN-008 —
    ZERO splits inside a function signature or a DDL statement, checked by
    re-parsing the source and asserting every signature/statement stays intact in
    a single chunk (tested at the production target AND at a tiny target that
    forces many boundaries).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from tree_sitter import Node
from tree_sitter_language_pack import SupportedLanguage, get_parser

from titan.memory.chunker import Chunk, ChunkContext, count_tokens, get_chunker, tail_tokens
from titan.memory.chunkers import (
    CodeChunker,
    ConversationChunker,
    MarkdownChunker,
    TableChunker,
    split_sql_statements,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "chunkers"


@dataclass(frozen=True)
class Case:
    fixture: str
    content_type: str
    ctx: ChunkContext
    budget: int


CASES: tuple[Case, ...] = (
    Case(
        "markdown_doc.md",
        "markdown",
        ChunkContext("org", "semantic", "Retrieval pipeline lessons"),
        512,
    ),
    Case(
        "code_python.py",
        "code",
        ChunkContext("org", "procedural", "retrieval planner (python)"),
        400,
    ),
    Case(
        "code_typescript.ts",
        "code",
        ChunkContext("org", "procedural", "retrieval planner (ts)"),
        400,
    ),
    Case("code_sql.sql", "code", ChunkContext("org", "procedural", "memory schema (sql)"), 400),
    Case(
        "conversation.txt",
        "conversation",
        ChunkContext("project:p1", "episodic", "retrieval regression triage"),
        512,
    ),
    Case(
        "table_markdown.md", "table", ChunkContext("org", "semantic", "agent authority matrix"), 384
    ),
    Case(
        "ddl_dump.sql", "table", ChunkContext("org", "procedural", "conflict subsystem schema"), 384
    ),
)
_BY_FIXTURE = {c.fixture: c for c in CASES}


def _fixture_text(fixture: str) -> str:
    return (GOLDEN_DIR / fixture).read_text(encoding="utf-8")


def _chunk(case: Case) -> list[Chunk]:
    return get_chunker(case.content_type).chunk(_fixture_text(case.fixture), case.ctx)


def _body(chunk: Chunk) -> str:
    """The chunk body — everything after the contextual header line."""
    return chunk.text.split("\n", 1)[1]


def _serialise(chunks: list[Chunk]) -> list[dict[str, object]]:
    return [{"seq": c.seq, "token_count": c.token_count, "text": c.text} for c in chunks]


# --- golden files -----------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.fixture)
def test_matches_golden(case: Case) -> None:
    golden_path = GOLDEN_DIR / f"{case.fixture}.golden.json"
    assert golden_path.exists(), f"missing golden for {case.fixture}; run the module to regenerate"
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    assert _serialise(_chunk(case)) == expected


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.fixture)
def test_every_chunk_has_the_contextual_header(case: Case) -> None:
    chunks = _chunk(case)
    n = len(chunks)
    for seq, chunk in enumerate(chunks):
        assert chunk.seq == seq  # contiguous from 0
        header = f"[{case.ctx.scope} | {case.ctx.kind} | {case.ctx.title} | chunk {seq}/{n}]"
        assert chunk.text.startswith(header + "\n")
        assert chunk.token_count == count_tokens(chunk.text)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.fixture)
def test_fixture_splits_into_multiple_chunks(case: Case) -> None:
    # Each fixture is sized to exercise a real boundary at its §5.2 target — a
    # single-chunk fixture would prove nothing about boundaries.
    assert len(_chunk(case)) >= 2


# --- size / overlap invariants (§5.2 matrix) --------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.fixture)
def test_chunk_bodies_are_within_budget(case: Case) -> None:
    # No chunk exceeds its content type's token budget. (Atomic units larger than
    # the budget would be their own oversize chunk; the fixtures contain none.)
    for chunk in _chunk(case):
        assert count_tokens(_body(chunk)) <= case.budget


def test_markdown_has_64_token_overlap_between_chunks() -> None:
    case = _BY_FIXTURE["markdown_doc.md"]
    chunks = _chunk(case)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        seed = tail_tokens(_body(prev), 64)  # §5.2: 64-token overlap
        assert seed and _body(nxt).startswith(seed)


def test_conversation_has_one_turn_overlap_between_chunks() -> None:
    case = _BY_FIXTURE["conversation.txt"]
    chunker = get_chunker("conversation")
    assert isinstance(chunker, ConversationChunker)
    chunks = _chunk(case)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        prev_turns = chunker._turns(_body(prev))
        last_turn = prev_turns[-1]  # §5.2: 1-turn overlap
        assert _body(nxt).startswith(last_turn)


def test_code_has_no_overlap_and_is_lossless() -> None:
    # Code uses unit boundaries with zero overlap, so the ordered bodies are a
    # contiguous, lossless partition of the source (concatenate back to identical).
    for fixture in ("code_python.py", "code_typescript.ts", "code_sql.sql"):
        case = _BY_FIXTURE[fixture]
        bodies = [_body(c) for c in _chunk(case)]
        assert "".join(bodies) == _fixture_text(fixture)


def test_table_repeats_header_in_every_chunk() -> None:
    case = _BY_FIXTURE["table_markdown.md"]
    lines = _fixture_text(case.fixture).splitlines()
    header = "\n".join(lines[:2])  # column row + separator row
    chunks = _chunk(case)
    for chunk in chunks:
        assert _body(chunk).startswith(header)


# --- TITAN-BRN-008: zero mid-signature / mid-DDL-statement splits -----------


def _walk(node: Node) -> list[Node]:
    out: list[Node] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(cur.children)
    return out


def _signature_spans(source: str, lang: SupportedLanguage) -> list[str]:
    """Every function/class/method signature text (declaration up to its body) —
    the substrings that must NEVER be split by a chunk boundary."""
    data = source.encode("utf-8")
    root = get_parser(lang).parse(data).root_node
    defn_types = {
        "function_definition",
        "class_definition",
        "function_declaration",
        "method_definition",
        "class_declaration",
    }
    signatures: list[str] = []
    for node in _walk(root):
        if node.type in defn_types:
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            sig = data[node.start_byte : end].decode("utf-8").strip()
            if sig:
                signatures.append(sig)
    return signatures


def _sql_statements(source: str) -> list[str]:
    data = source.encode("utf-8")
    root = get_parser("sql").parse(data).root_node
    return [
        data[n.start_byte : n.end_byte].decode("utf-8").strip()
        for n in root.children
        if n.type == "statement"
    ]


@pytest.mark.parametrize(
    ("fixture", "lang"),
    [("code_python.py", "python"), ("code_typescript.ts", "typescript")],
)
@pytest.mark.parametrize("target", [400, 40])
def test_no_signature_is_ever_split(fixture: str, lang: SupportedLanguage, target: int) -> None:
    """A function/class signature is never divided by a chunk boundary — at the
    production target AND at a tiny target that forces many internal splits."""
    source = _fixture_text(fixture)
    ctx = _BY_FIXTURE[fixture].ctx
    bodies = [_body(c) for c in CodeChunker(target_tokens=target).chunk(source, ctx)]
    signatures = _signature_spans(source, lang)
    assert signatures  # the fixture actually contains definitions
    for sig in signatures:
        assert any(sig in body for body in bodies), f"signature split at target {target}: {sig!r}"


@pytest.mark.parametrize("fixture", ["code_sql.sql", "ddl_dump.sql"])
@pytest.mark.parametrize("target", [400, 40])
def test_no_ddl_statement_is_ever_split_or_duplicated(fixture: str, target: int) -> None:
    """A whole DDL statement is never divided — and appears in EXACTLY one chunk
    (a statement absorbed into a repeated header would show up in every chunk).
    Proven for both the SQL-source (code) chunker and the schema-dump (table)
    chunker, at the production target and a tiny one."""
    source = _fixture_text(fixture)
    ctx = _BY_FIXTURE[fixture].ctx
    content_type = _BY_FIXTURE[fixture].content_type
    if content_type == "code":
        chunker: object = CodeChunker(target_tokens=target)
    else:
        chunker = TableChunker(target_tokens=target)
    assert isinstance(chunker, (CodeChunker, TableChunker))
    bodies = [_body(c) for c in chunker.chunk(source, ctx)]
    statements = _sql_statements(source)
    assert statements
    for stmt in statements:
        hits = sum(body.count(stmt) for body in bodies)
        assert hits == 1, f"DDL split/duplicated (x{hits}) at target {target}: {stmt[:60]!r}"


def test_ddl_leading_comments_never_absorb_the_first_statement() -> None:
    # The repeated header is ONLY the raw leading comment lines; the first CREATE
    # (glued to those comments by the ';'-split) must appear exactly once.
    chunks = _chunk(_BY_FIXTURE["ddl_dump.sql"])
    assert len(chunks) >= 2
    bodies = [_body(c) for c in chunks]
    assert sum(b.count("CREATE TABLE conflict_transitions") for b in bodies) == 1
    for body in bodies:  # every chunk still carries the comment header
        assert body.startswith("-- Schema dump for the conflict subsystem.")


def test_semicolons_inside_dollar_quotes_strings_and_comments_do_not_split() -> None:
    ctx = ChunkContext("org", "procedural", "functions")
    dump = (
        "CREATE FUNCTION notify_change() RETURNS trigger AS $fn$\n"
        "BEGIN\n"
        "  PERFORM pg_notify('chan', 'a;b'); RETURN NEW;\n"
        "END;\n"
        "$fn$ LANGUAGE plpgsql;\n"
        "-- a comment with a ; semicolon\n"
        "CREATE TABLE t (s text DEFAULT 'x;y', \"col;on\" int); /* block ; */\n"
        "CREATE INDEX ix_t ON t (s);\n"
    )
    stmts = split_sql_statements(dump)
    assert len(stmts) == 3  # the function body's inner ';' never terminates it
    assert stmts[0].endswith("$fn$ LANGUAGE plpgsql;")
    # End-to-end through the table chunker at a tiny target: each statement intact.
    bodies = [_body(c) for c in TableChunker(target_tokens=20).chunk(dump, ctx)]
    for stmt in stmts:
        assert sum(b.count(stmt) for b in bodies) == 1


def test_pg_dump_banner_comments_do_not_hijack_ddl_into_the_table_path() -> None:
    # A pg_dump-style banner ('--' lines) plus any '|' in the SQL (e.g. string
    # concatenation) must NOT read as a markdown table — the line-wise table path
    # would split multi-line statements (TITAN-BRN-008). A separator row counts
    # only when it contains a real '|'.
    ctx = ChunkContext("org", "procedural", "dump")
    dump = (
        "--\n"
        "-- PostgreSQL database dump\n"
        "--\n"
        "CREATE TABLE users (\n"
        "    id integer NOT NULL,\n"
        "    display text GENERATED ALWAYS AS (first || ' ' || last) STORED\n"
        ");\n"
        "CREATE INDEX ix_users ON users (id);\n"
    )
    chunker = TableChunker(target_tokens=12)  # tiny: the markdown path WOULD split
    assert chunker._detect(dump) == "ddl"
    bodies = [_body(c) for c in chunker.chunk(dump, ctx)]
    for stmt in split_sql_statements(dump):
        if not stmt.lstrip().startswith("--"):
            assert sum(b.count(stmt) for b in bodies) == 1, f"split: {stmt[:50]!r}"


def test_action_log_csv_is_not_misread_as_ddl() -> None:
    # Rows starting with bare SQL verbs (DELETE,101,...) are CSV, not DDL — a
    # DDL misdetect would emit the whole file as one unbounded 'statement'.
    ctx = ChunkContext("org", "semantic", "audit log")
    csv = "action,target,qty\n" + "\n".join(f"DELETE,user{i},{i}" for i in range(60))
    chunker = TableChunker(target_tokens=40)
    assert chunker._detect(csv) == "csv"
    chunks = chunker.chunk(csv, ctx)
    assert len(chunks) >= 2  # grouped to budget, not one giant chunk
    for chunk in chunks:
        assert _body(chunk).startswith("action,target,qty")  # header repeated


def test_nested_block_comments_do_not_split_a_statement() -> None:
    # PostgreSQL nests /* */ — a ';' inside the OUTER comment is not a terminator.
    dump = (
        "CREATE TABLE t (\n  a int /* note /* nested */ ; tricky */\n);\nCREATE INDEX i ON t (a);\n"
    )
    stmts = split_sql_statements(dump)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE t") and stmts[0].endswith(");")


def test_e_string_backslash_escapes_do_not_split_a_statement() -> None:
    # E'…' strings escape quotes with backslash; the ';' inside stays quoted.
    dump = "INSERT INTO t VALUES (E'it\\'s; fine');\nCREATE TABLE u (x int);\n"
    stmts = split_sql_statements(dump)
    assert len(stmts) == 2
    assert stmts[0] == "INSERT INTO t VALUES (E'it\\'s; fine');"
    assert stmts[1] == "CREATE TABLE u (x int);"


def test_crlf_markdown_table_still_splits_with_repeated_header() -> None:
    # Windows line endings must not defeat the separator match (which would
    # collapse the table into one unbounded chunk).
    ctx = ChunkContext("org", "semantic", "crlf table")
    table = "\r\n".join(
        ["| id | name |", "|----|------|"] + [f"| {i} | row{i}name |" for i in range(60)]
    )
    chunks = TableChunker(target_tokens=40).chunk(table, ctx)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _body(chunk).startswith("| id | name |\n|----|------|")


def test_comment_ruler_with_pipes_does_not_hijack_ddl() -> None:
    # A column legend drawn in '--' comments ('-- |----|----|') is a comment,
    # not markdown-table evidence, when the text carries DDL statements.
    ctx = ChunkContext("org", "procedural", "legend dump")
    dump = (
        "-- Legend:\n"
        "-- | column | meaning |\n"
        "-- |--------|---------|\n"
        "CREATE TABLE t (\n  a int,\n  b text\n);\n"
        "CREATE INDEX i ON t (a);\n"
    )
    chunker = TableChunker(target_tokens=12)  # tiny: the markdown path WOULD split
    assert chunker._detect(dump) == "ddl"
    bodies = [_body(c) for c in chunker.chunk(dump, ctx)]
    stmt = "CREATE TABLE t (\n  a int,\n  b text\n);"
    assert sum(b.count(stmt) for b in bodies) == 1


@pytest.mark.parametrize(
    "head",
    [
        "CREATE UNLOGGED TABLE audit",
        "CREATE FOREIGN TABLE remote_users",
        "CREATE OR REPLACE VIEW v_active",
        "CREATE UNIQUE INDEX ix_x ON t",
        "GRANT SELECT, INSERT ON TABLE billing TO analyst",
        "REVOKE ALL ON SCHEMA app FROM public",
        "TRUNCATE TABLE staging_events",
        "COMMENT ON COLUMN t.a IS 'meaning'",
        "COPY app.users (id, name) FROM stdin",
    ],
)
def test_ddl_statement_heads_are_recognised(head: str) -> None:
    # Every realistic dump head must be DDL evidence — a miss demotes the dump to
    # the plain window path, which splits statements (TITAN-BRN-008).
    assert TableChunker()._detect(head + " ...") == "ddl", head


def test_query_log_csv_with_statement_cells_is_still_csv() -> None:
    # pg_stat_statements-style exports carry whole statements in a CSV cell; the
    # comma-bearing header row must beat the DDL evidence in data rows.
    ctx = ChunkContext("org", "semantic", "slow queries")
    csv = "statement,calls,total_ms\n" + "\n".join(
        f"UPDATE orders SET status = 'shipped' WHERE id = {i},{i},0.5" for i in range(50)
    )
    chunker = TableChunker(target_tokens=120)
    assert chunker._detect(csv) == "csv"
    chunks = chunker.chunk(csv, ctx)
    assert len(chunks) >= 2  # grouped, never one unbounded chunk
    for chunk in chunks:
        assert _body(chunk).startswith("statement,calls,total_ms")


def test_aligned_column_table_splits_row_atomically_with_header() -> None:
    # sqlite3 -column / psql style output: dash-run separator, no pipes. Must
    # route through the row-atomic table path, not plain windows.
    ctx = ChunkContext("org", "semantic", "cli table")
    header = "id          name        state\n----------  ----------  ----------"
    table = header + "\n" + "\n".join(f"{i:<10}  row{i:<7}  active" for i in range(60))
    chunker = TableChunker(target_tokens=60)
    assert chunker._detect(table) == "markdown"
    chunks = chunker.chunk(table, ctx)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _body(chunk).startswith(header)


def test_begin_atomic_function_body_is_one_statement() -> None:
    # PG14+ SQL-standard bodies have NO dollar quoting; interior ';' (and a
    # CASE…END inside) must not terminate the CREATE FUNCTION.
    dump = (
        "CREATE FUNCTION add_audit(x int) RETURNS int\n"
        "LANGUAGE SQL\n"
        "BEGIN ATOMIC\n"
        "  INSERT INTO audit VALUES (x);\n"
        "  SELECT CASE WHEN x > 0 THEN x ELSE 0 END;\n"
        "END;\n"
        "CREATE TABLE t2 (a int);\n"
    )
    stmts = split_sql_statements(dump)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE FUNCTION") and stmts[0].endswith("END;")
    assert stmts[1] == "CREATE TABLE t2 (a int);"


def test_unterminated_ddl_is_detected_and_kept_whole() -> None:
    # No trailing ';' must not demote the dump to the plain window path (which
    # would split mid-statement — TITAN-BRN-008).
    ctx = ChunkContext("org", "procedural", "schema")
    stmt = "CREATE TABLE wide (" + ", ".join(f"col{i} text" for i in range(200)) + ")"
    chunks = TableChunker(target_tokens=50).chunk(stmt, ctx)
    assert len(chunks) == 1  # one oversize but INTACT statement
    assert _body(chunks[0]) == stmt


def test_markdown_target_is_a_hard_cap_for_any_paragraph_mix() -> None:
    # Near-target paragraphs force the overlap seed to shrink: no body may exceed
    # the target (the review showed the old seed could push bodies to target+64).
    ctx = ChunkContext("org", "semantic", "cap")
    chunker = MarkdownChunker(target_tokens=50, overlap_tokens=10)
    near = "\n\n".join(" ".join(f"w{j}p{i}" for i in range(45)) for j in range(4))
    giant = " ".join(f"g{i}" for i in range(300))  # oversize single paragraph
    for doc in (near, giant, near + "\n\n" + giant):
        for chunk in chunker.chunk(doc, ctx):
            assert count_tokens(_body(chunk)) <= 50


def test_markdown_oversize_paragraph_split_does_not_double_overlap() -> None:
    # Windowing an oversize paragraph uses NO internal overlap (the packer seeds
    # the §5.2 overlap), so total duplication is bounded by overlap per boundary.
    ctx = ChunkContext("org", "semantic", "dup")
    chunker = MarkdownChunker(target_tokens=50, overlap_tokens=10)
    giant = " ".join(f"u{i}" for i in range(240))
    chunks = chunker.chunk(giant, ctx)
    total = sum(count_tokens(_body(c)) for c in chunks)
    assert total <= 240 + (len(chunks) - 1) * 10  # source + at most one seed per boundary


def test_conversation_overlap_dropped_when_it_would_blow_the_budget() -> None:
    # Three ~48-token turns at target 50: repeating the previous turn would give
    # ~96-token bodies, so the seed is dropped and each turn is its own chunk.
    ctx = ChunkContext("org", "episodic", "big turns")
    turns = "".join(f"S{k}: " + " ".join(f"t{k}w{i}" for i in range(46)) + "\n" for k in range(3))
    chunks = ConversationChunker(target_tokens=50).chunk(turns, ctx)
    assert len(chunks) == 3
    for chunk in chunks:
        assert count_tokens(_body(chunk)) <= 50


# --- markdown heading awareness --------------------------------------------


def test_markdown_prefers_heading_boundaries() -> None:
    # With a heading-sized budget, a document split at headings never orphans a
    # heading marker at the very end of a chunk (the split lands before it).
    doc = (
        "## Section one\nAlpha beta gamma delta epsilon zeta eta theta.\n\n"
        "## Section two\nOne two three four five six seven eight nine ten.\n\n"
        "## Section three\nRed orange yellow green blue indigo violet white."
    )
    ctx = ChunkContext("org", "semantic", "Headings")
    chunks = MarkdownChunker(target_tokens=20, overlap_tokens=4).chunk(doc, ctx)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert not _body(chunk).rstrip().endswith("##")


# --- regeneration -----------------------------------------------------------


def regenerate() -> None:
    """(Re)write every golden file from the current chunker output. Run as a
    script after an intended chunking change; review the diff before committing."""
    for case in CASES:
        payload = _serialise(_chunk(case))
        path = GOLDEN_DIR / f"{case.fixture}.golden.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}  ({len(payload)} chunks)")


if __name__ == "__main__":
    regenerate()
