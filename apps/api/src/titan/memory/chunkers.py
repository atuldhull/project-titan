"""The four content-aware chunkers (Vol 5 §5.2, TITAN-BRN-008), selected by the
``content_type`` the writer declares on each Memory Event. Each implements the
:class:`~titan.memory.chunker.Chunker` interface and is registered against its
content type by :func:`register_builtin_chunkers` (called from
:mod:`titan.memory.chunker`); the fixed-size fallback there covers unknown types.

  * Markdown / prose — heading-aware recursive splitter (split at ``##``/``###``,
    then paragraphs); 512-token target, 64-token overlap.
  * Source code — syntax-aware via tree-sitter (TypeScript, Python, SQL); whole
    functions/classes as units, 400-token target, no overlap; an oversize unit is
    split only at STATEMENT boundaries, never inside a function signature or a DDL
    statement (TITAN-BRN-008).
  * Conversation / transcript — turn-grouped to a 512-token budget, 1-turn overlap.
  * Table / schema — never splits a row or DDL statement; groups rows to ~384
    tokens, repeating the column header in every chunk.

Body segmentation happens here; the contextual header prefix and seq/n numbering
are added uniformly by :func:`~titan.memory.chunker.assemble`.
"""

import re
from collections.abc import Sequence

from tree_sitter import Node
from tree_sitter_language_pack import SupportedLanguage, get_parser

from titan.memory.chunker import (
    Chunk,
    ChunkContext,
    assemble,
    count_tokens,
    register_chunker,
    tail_tokens,
    window_by_tokens,
)

# --- 1. Markdown / prose ---------------------------------------------------

_MD_HEADING = re.compile(r"^[ \t]*#{2,3}[ \t]", re.MULTILINE)
_MD_PARA_BREAK = re.compile(r"\n[ \t]*\n")


class MarkdownChunker:
    """Heading-aware recursive splitter (§5.2). Splits at ``##``/``###`` headings,
    then paragraphs; packs paragraphs to ``target_tokens`` and seeds each next
    chunk with the trailing ``overlap_tokens`` of the previous one so context
    carries across the boundary. An oversize single paragraph is hard-split into
    ``target - overlap``-token windows as a last resort (no internal overlap —
    the packer's seed supplies the §5.2 overlap at every chunk boundary).

    The target is a HARD cap on the chunk body: the overlap seed is shrunk (down
    to zero) when the next paragraph would not otherwise fit, so no markdown
    chunk body ever exceeds ``target_tokens`` — overlap is best-effort context
    carry, the budget protects the embedder's context window."""

    def __init__(self, target_tokens: int = 512, overlap_tokens: int = 64) -> None:
        if overlap_tokens >= target_tokens:
            raise ValueError("overlap_tokens must be < target_tokens")
        self._target = target_tokens
        self._overlap = overlap_tokens
        # The module token definition, ALWAYS: window_by_tokens/tail_tokens size
        # pieces and seeds in the same unit, so an injected counter would break
        # the hard-cap arithmetic with mixed units. Swapping the tokenizer
        # (TITAN-BRN-006) happens at the chunker-module seam, not per instance.
        self._count = count_tokens

    def _units(self, text: str) -> list[str]:
        """Paragraph units: split into sections at ``##``/``###`` headings, then
        each section into paragraphs on blank lines. A section heading stays
        attached to the first paragraph of its section (its context)."""
        starts = sorted({0, *(m.start() for m in _MD_HEADING.finditer(text))})
        units: list[str] = []
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(text)
            section = text[s:e]
            idx = 0
            for m in _MD_PARA_BREAK.finditer(section):
                para = section[idx : m.start()]
                if para.strip():
                    units.append(para.strip("\n"))
                idx = m.end()
            tail = section[idx:]
            if tail.strip():
                units.append(tail.strip("\n"))
        return units

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
        # Expand oversize paragraphs into token windows with NO internal overlap
        # (the packer seeds the §5.2 overlap at chunk boundaries — windowing with
        # overlap here would double it) sized so a seed + window still fits the
        # target.
        pieces: list[str] = []
        for unit in self._units(text):
            if self._count(unit) > self._target:
                pieces.extend(window_by_tokens(unit, self._target - self._overlap, 0))
            else:
                pieces.append(unit)

        bodies: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        has_real = False  # only real content triggers a flush, never a lone seed
        for piece in pieces:
            pt = self._count(piece)
            if has_real and cur_tokens + pt > self._target:
                body = "\n\n".join(cur)
                bodies.append(body)
                # Overlap seed, shrunk so seed + the incoming piece still fits the
                # target (the budget is a hard cap; overlap is best-effort).
                seed_budget = min(self._overlap, max(0, self._target - pt))
                seed = tail_tokens(body, seed_budget)
                cur = [seed] if seed else []
                cur_tokens = self._count(seed) if seed else 0
                has_real = False
            cur.append(piece)
            cur_tokens += pt
            has_real = True
        if has_real:
            bodies.append("\n\n".join(cur))
        return assemble(bodies, ctx)


# --- 2. Source code (tree-sitter) ------------------------------------------

# Priority order for language auto-detection: the grammar that parses the body
# with the fewest error nodes wins; ties fall to this order.
_CODE_LANGUAGES: tuple[SupportedLanguage, ...] = ("python", "typescript", "sql")


def _count_errors(root: Node) -> int:
    """Total ERROR + MISSING nodes in the parse tree — the syntax-fit score for
    language auto-detection (lower is a better fit)."""
    errors = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            errors += 1
        stack.extend(node.children)
    return errors


def _find_body(node: Node) -> Node | None:
    """The block/statement-list body of a definition, unwrapping one level of
    wrappers (``export_statement``, ``decorated_definition``) so an exported or
    decorated function's body is still found. ``None`` when the node has no
    splittable body (e.g. a single DDL statement) — such units are kept whole."""
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.named_children:
        body = child.child_by_field_name("body")
        if body is not None:
            return body
    return None


class CodeChunker:
    """Syntax-aware code chunker (§5.2, TITAN-BRN-008). Detects the language
    (TypeScript / Python / SQL), then packs whole top-level definitions/statements
    to ``target_tokens`` with no overlap. A single definition larger than the
    target is split ONLY at the boundaries between its body statements, so a
    function signature — or a whole DDL statement — is never divided. Chunk bodies
    are exact source substrings (byte-accurate), so downstream tooling and the
    golden tests see verbatim code."""

    def __init__(
        self,
        target_tokens: int = 400,
        languages: Sequence[SupportedLanguage] = _CODE_LANGUAGES,
    ) -> None:
        self._target = target_tokens
        self._count = count_tokens  # the module token definition (see MarkdownChunker)
        self._languages = tuple(languages)

    def _detect(self, data: bytes) -> Node:
        best: tuple[int, Node] | None = None
        for lang in self._languages:
            root = get_parser(lang).parse(data).root_node
            errors = _count_errors(root)
            if best is None or errors < best[0]:
                best = (errors, root)
            if errors == 0:
                break  # a clean parse wins immediately (respects priority order)
        assert best is not None  # noqa: S101 — _languages is non-empty
        return best[1]

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
        data = text.encode("utf-8")
        if not data.strip():
            return assemble([], ctx)
        try:
            root = self._detect(data)
            top = [c for c in root.children if c.is_named]
        except Exception:  # noqa: BLE001 — any parser failure degrades gracefully
            top = []
        if not top:
            # Unparseable / no top-level constructs — fall back to token windows.
            return assemble(window_by_tokens(text, self._target, 0), ctx)

        # Contiguous unit spans at named-node boundaries: unit i owns everything
        # from its start up to the next unit's start (trailing punctuation, blank
        # lines), so the slices are lossless and every cut lands on a boundary.
        spans: list[tuple[int, int, Node]] = []
        for i, node in enumerate(top):
            start = node.start_byte
            end = top[i + 1].start_byte if i + 1 < len(top) else len(data)
            spans.append((start, end, node))
        spans[0] = (0, spans[0][1], spans[0][2])  # keep leading trivia

        bodies = [data[s:e].decode("utf-8", "replace") for s, e in self._pack(spans, data)]
        return assemble(bodies, ctx)

    def _pack(self, spans: list[tuple[int, int, Node]], data: bytes) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        cur: tuple[int, int] | None = None
        cur_tokens = 0
        for start, end, node in spans:
            tokens = self._count(data[start:end].decode("utf-8", "replace"))
            if tokens > self._target:
                if cur is not None:
                    out.append(cur)
                    cur, cur_tokens = None, 0
                out.extend(self._split_oversize(node, start, end, data))
                continue
            if cur is None:
                cur, cur_tokens = (start, end), tokens
            elif cur_tokens + tokens <= self._target:
                cur, cur_tokens = (cur[0], end), cur_tokens + tokens
            else:
                out.append(cur)
                cur, cur_tokens = (start, end), tokens
        if cur is not None:
            out.append(cur)
        return out

    def _split_oversize(
        self, node: Node, start: int, end: int, data: bytes
    ) -> list[tuple[int, int]]:
        """Split one oversize definition at its body-statement boundaries. The
        signature (start → first statement) stays with the first group; cuts only
        ever fall at a statement's end. A node with no splittable body (a DDL
        statement) is returned whole — never divided (TITAN-BRN-008)."""
        body = _find_body(node)
        statements = body.named_children if body is not None else []
        if not statements:
            return [(start, end)]
        out: list[tuple[int, int]] = []
        group_start = start
        prev_end: int | None = None
        for stmt in statements:
            group_tokens = self._count(data[group_start : stmt.end_byte].decode("utf-8", "replace"))
            if group_tokens > self._target and prev_end is not None:
                out.append((group_start, prev_end))
                group_start = prev_end
            prev_end = stmt.end_byte
        out.append((group_start, end))  # final group runs to the unit end (closing brace + trailer)
        return out


# --- 3. Conversation / transcript ------------------------------------------

_TURN_START = re.compile(r"^[ \t]*[^\n:]{1,60}:[ \t]", re.MULTILINE)


class ConversationChunker:
    """Turn-grouped transcript chunker (§5.2). A turn begins at a ``Speaker: ``
    line and runs to the next such line. Consecutive turns accumulate until the
    ``target_tokens`` budget; each next chunk repeats the previous chunk's last
    turn (1-turn overlap). A single turn larger than the budget is its own chunk —
    a turn is atomic and never split.

    The overlap turn is repeated only when it fits the budget alongside the
    incoming turn; otherwise it is dropped for that boundary, so a chunk body
    exceeds ``target_tokens`` only when a SINGLE turn alone does (the budget
    protects the embedder's context window; overlap is best-effort)."""

    def __init__(
        self,
        target_tokens: int = 512,
    ) -> None:
        self._target = target_tokens
        self._count = count_tokens  # the module token definition (see MarkdownChunker)

    def _turns(self, text: str) -> list[str]:
        starts = [m.start() for m in _TURN_START.finditer(text)]
        if not starts:
            return [text] if text.strip() else []
        turns: list[str] = []
        preamble = text[: starts[0]]
        if preamble.strip():
            turns.append(preamble)
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(text)
            turns.append(text[s:e])
        return turns

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
        bodies: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        has_real = False
        for turn in self._turns(text):
            tt = self._count(turn)
            if has_real and cur_tokens + tt > self._target:
                bodies.append("".join(cur))
                # 1-turn overlap: repeat the last turn as the seed — but only when
                # it fits the budget with the incoming turn, so overlap never
                # pushes a multi-turn chunk past the target.
                last = cur[-1]
                lt = self._count(last)
                if lt + tt <= self._target:
                    cur = [last]
                    cur_tokens = lt
                else:
                    cur = []
                    cur_tokens = 0
                has_real = False
            cur.append(turn)
            cur_tokens += tt
            has_real = True
        if has_real:
            bodies.append("".join(cur))
        return assemble(bodies, ctx)


# --- 4. Table / schema -----------------------------------------------------

# A markdown-table separator row. Callers must ALSO require a '|' in the line:
# without one, a bare '--'/'-- ----' SQL comment or a '---' thematic break would
# match and hijack a DDL dump into the table path (whose line-wise rows would
# then split statements — the exact TITAN-BRN-008 failure the DDL path prevents).
_MD_SEPARATOR = re.compile(r"^[ \t]*\|?[ \t:|-]*-{2,}[ \t:|-]*\|?[ \t]*$")
# A DDL/DML dump: some line STARTS with a full statement head (line-anchored,
# multi-word — a bare verb would misfire on an action-log CSV like 'DELETE,101,2'
# or prose like 'Update: ...'). No ';' is required: a dump whose last/only
# statement lacks a terminator must still be treated as DDL, or the plain-window
# fallback would split it mid-statement (TITAN-BRN-008).
_DDL_LINE = re.compile(
    r"^[ \t]*(?:"
    r"(?:CREATE|ALTER|DROP)\s+(?:OR\s+REPLACE\s+)?"
    r"(?:UNIQUE\s+|UNLOGGED\s+|FOREIGN\s+|MATERIALIZED\s+|RECURSIVE\s+|EVENT\s+"
    r"|GLOBAL\s+|LOCAL\s+|TEMPORARY\s+|TEMP\s+)*"
    r"(?:TABLE|INDEX|VIEW|SCHEMA|TYPE|FUNCTION|PROCEDURE|TRIGGER|SEQUENCE|EXTENSION"
    r"|DATABASE|ROLE|USER|POLICY|DOMAIN)\b"
    r"|INSERT\s+INTO\b|DELETE\s+FROM\b|UPDATE\s+\S+\s+SET\b"
    r"|TRUNCATE\s+(?:TABLE\s+|ONLY\s+)?\w+|COMMENT\s+ON\b|GRANT\s+\w+|REVOKE\s+\w+"
    r"|COPY\s+\S+\s+(?:.*\s)?(?:FROM|TO)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# An aligned-column separator (sqlite3 -column / psql / pandoc simple tables):
# runs of 4+ dashes separated by spaces. Weaker evidence than a piped separator
# ('--'/'---' comment markers and thematic breaks must not match), so it is
# consulted only after the DDL check.
_COLUMN_SEPARATOR = re.compile(r"^[ \t]*-{4,}(?:[ \t]+-{4,})*[ \t]*$")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")
_WORD = re.compile(r"\w+")


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _is_md_separator(line: str) -> bool:
    """A markdown-table separator row — must contain a '|' so bare '--' SQL
    comments and '---' thematic breaks never count as table evidence."""
    return "|" in line and _MD_SEPARATOR.match(line) is not None


def split_sql_statements(text: str) -> list[str]:
    """Split a SQL dump at TOP-LEVEL ``;`` only — never inside a single-quoted
    string (``''`` doubling; backslash escapes in ``E'…'`` strings), a
    double-quoted identifier, a dollar-quoted body (``$$…$$`` / ``$tag$…$tag$``),
    a ``--``/``/* */`` comment (block comments NEST, per PostgreSQL), or a
    PG14+ SQL-standard ``BEGIN ATOMIC … END`` function body — so a whole
    statement (e.g. a ``CREATE FUNCTION`` with semicolons in its body) is never
    divided (TITAN-BRN-008). An unterminated trailing statement is kept whole."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    # BEGIN ATOMIC nesting: ';' inside an atomic body is not a terminator. CASE
    # increments too (only while inside a body) because its END would otherwise
    # close the body early — CASE…END pairs are balanced in valid SQL.
    depth = 0
    prev_word = ""

    def flush() -> None:
        stmt = "".join(buf).strip("\n")
        if stmt.strip():
            statements.append(stmt)
        buf.clear()

    while i < n:
        ch = text[i]
        if ch == "'":  # string literal; '' is an escaped quote, not a terminator
            # E'…' escape strings also honor backslash escapes (PostgreSQL).
            escape_string = i > 0 and text[i - 1] in "eE" and (i < 2 or not _is_word(text[i - 2]))
            j = i + 1
            while j < n:
                if escape_string and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            buf.append(text[i:j])
            i = j
        elif ch == '"':  # quoted identifier
            j = text.find('"', i + 1)
            j = n if j == -1 else j + 1
            buf.append(text[i:j])
            i = j
        elif text.startswith("--", i):  # line comment
            j = text.find("\n", i)
            j = n if j == -1 else j
            buf.append(text[i:j])
            i = j
        elif text.startswith("/*", i):  # block comment — PostgreSQL NESTS these
            depth = 1
            j = i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            buf.append(text[i:j])
            i = j
        elif ch == "$" and (m := _DOLLAR_TAG.match(text, i)):  # dollar-quoted body
            tag = m.group(0)
            j = text.find(tag, i + len(tag))
            j = n if j == -1 else j + len(tag)
            buf.append(text[i:j])
            i = j
        elif (w := _WORD.match(text, i)) is not None:  # keyword tracking
            word = w.group(0)
            buf.append(word)
            i = w.end()
            upper = word.upper()
            if (upper == "ATOMIC" and prev_word == "BEGIN") or (upper == "CASE" and depth > 0):
                depth += 1
            elif upper == "END" and depth > 0:
                depth -= 1
            prev_word = upper
        else:
            buf.append(ch)
            i += 1
            if ch == ";" and depth == 0:
                flush()
    flush()
    return statements


class TableChunker:
    """Row/statement-grouped chunker for tables and schemas (§5.2). Never splits a
    row or a DDL statement: it groups atomic rows to ``target_tokens``, repeating
    the column header in every chunk so an isolated chunk stays self-describing. A
    single row/statement larger than the target is its own (intact) chunk.
    Recognises Markdown tables, CSV, and DDL/DML dumps (terminated or not)."""

    def __init__(
        self,
        target_tokens: int = 384,
    ) -> None:
        self._target = target_tokens
        self._count = count_tokens  # the module token definition (see MarkdownChunker)

    def _detect(self, text: str) -> str:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        has_ddl = _DDL_LINE.search(text) is not None
        # Piped markdown separator — but a '--'-prefixed one is a SQL comment
        # ruler when the text also carries DDL evidence (a column legend drawn in
        # dump comments must not drag the dump onto the line-wise table path,
        # which would split statements — TITAN-BRN-008).
        if any(
            _is_md_separator(ln) and not (has_ddl and ln.lstrip().startswith("--")) for ln in lines
        ):
            return "markdown"
        csv_shape = (
            bool(lines)
            and lines[0].count(",") >= 1
            and sum("," in ln for ln in lines) >= max(2, len(lines) // 2)
        )
        # A comma-bearing header row beats DDL evidence found in DATA rows (a
        # query-log CSV legitimately has cells like 'DELETE FROM …'); a dump whose
        # FIRST line is itself a statement head stays DDL.
        if csv_shape and not _DDL_LINE.match(lines[0]):
            return "csv"
        if has_ddl:
            return "ddl"
        if any(_COLUMN_SEPARATOR.match(ln) for ln in lines):
            return "markdown"  # aligned-column table (sqlite3/psql/pandoc style)
        if csv_shape:
            return "csv"
        return "plain"

    def _group(self, header: str, rows: Sequence[str], joiner: str) -> list[str]:
        """Group atomic rows to the token budget, repeating ``header`` per chunk.
        A row is never split; a lone oversize row becomes its own chunk."""
        base = self._count(header) if header else 0
        prefix = f"{header}{joiner}" if header else ""
        bodies: list[str] = []
        cur: list[str] = []
        cur_tokens = base
        for row in rows:
            rt = self._count(row)
            if cur and cur_tokens + rt > self._target:
                bodies.append(prefix + joiner.join(cur))
                cur, cur_tokens = [], base
            cur.append(row)
            cur_tokens += rt
        if cur:
            bodies.append(prefix + joiner.join(cur))
        return bodies

    def _markdown(self, text: str) -> list[str]:
        lines = text.split("\n")
        header_lines: list[str] = []
        rows: list[str] = []
        seen_sep = False
        for ln in lines:
            if not ln.strip():
                continue
            if not seen_sep:
                header_lines.append(ln)
                if _is_md_separator(ln) or _COLUMN_SEPARATOR.match(ln):
                    seen_sep = True
            else:
                rows.append(ln)
        header = "\n".join(header_lines)
        return self._group(header, rows, "\n") if rows else [text.strip("\n")]

    def _csv(self, text: str) -> list[str]:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            return [text.strip("\n")]
        header, rows = lines[0], lines[1:]
        return self._group(header, rows, "\n") if rows else [header]

    def _ddl(self, text: str) -> list[str]:
        """Statement-grouped DDL/DML. The header is ONLY the leading comment
        lines of the raw text (peeled BEFORE statement-splitting, so a comment
        glued to the first statement can never drag that whole statement into
        the repeated header). Statements are split at top-level ``;`` only via
        :func:`split_sql_statements` — a statement is never divided, even with
        semicolons inside string literals or dollar-quoted bodies
        (TITAN-BRN-008)."""
        lines = text.split("\n")
        h = 0
        while h < len(lines) and (not lines[h].strip() or lines[h].lstrip().startswith("--")):
            h += 1
        header = "\n".join(ln for ln in lines[:h] if ln.strip())
        statements = split_sql_statements("\n".join(lines[h:]))
        return self._group(header, statements, "\n") if statements else [text.strip("\n")]

    def chunk(self, text: str, ctx: ChunkContext) -> list[Chunk]:
        # Normalise CRLF once: every splitter here is line-oriented on '\n', and
        # a '\r' left on line ends would defeat the $-anchored separator match
        # (a Windows-authored table would fall through as one unbounded chunk).
        text = text.replace("\r\n", "\n")
        fmt = self._detect(text)
        if fmt == "markdown":
            bodies = self._markdown(text)
        elif fmt == "ddl":
            bodies = self._ddl(text)
        elif fmt == "csv":
            bodies = self._csv(text)
        else:  # no recognised tabular structure — degrade to token windows
            bodies = window_by_tokens(text, self._target, 0)
        return assemble(bodies, ctx)


# --- registration ----------------------------------------------------------


def register_builtin_chunkers() -> None:
    """Bind the four §5.2 chunkers to their content types. Idempotent; called on
    import (below) so the registry is populated regardless of which of this module
    or :mod:`titan.memory.chunker` is imported first."""
    markdown = MarkdownChunker()
    register_chunker("markdown", markdown)
    register_chunker("prose", markdown)
    register_chunker("code", CodeChunker())
    register_chunker("conversation", ConversationChunker())
    register_chunker("table", TableChunker())


__all__ = [
    "CodeChunker",
    "ConversationChunker",
    "MarkdownChunker",
    "TableChunker",
    "register_builtin_chunkers",
    "split_sql_statements",
]

# Self-register on import so the registry is populated under either import order
# (this module first, or titan.memory.chunker first, which imports this one).
register_builtin_chunkers()
