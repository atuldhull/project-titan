"""Build LogQL from the viewer's filter bar. Pure: no I/O, no Loki.

This is where the awkward truths about querying *this* deployment's logs live,
so the client and the HTTP adapter never have to know them.

**Only three labels exist** (Vol 9 §6.3, and `infra/promtail/promtail-config.yaml`
really does implement it): ``namespace`` (always ``titan``), ``service`` (the
*Compose* service name — ``api``, ``worker``, …) and ``level``. Everything else
is inside the JSON body and costs a parse, so it goes after ``| json``.

The five rules that bite, each learned from a real config in this repo:

1. **``level`` and ``service`` collide.** promtail promotes both to stream
   labels, and structlog *also* writes both into the body. Loki renames a
   parsed field that clashes with an existing label to ``<name>_extracted``.
   So we never bare-``| json``-and-filter: every extracted field is named by
   us, prefixed ``f_`` (``| json f_project_id="project_id"``), which cannot
   collide with a label and so can never be renamed out from under a filter.
2. **Label matchers are fully anchored; line filters are not.** ``|~ "err"``
   is a substring match, but ``| level =~ "err"`` matches nothing — a label
   matcher must be ``.*err.*``. Same operator, opposite semantics.
3. **Levels are lowercase** — structlog writes the *method name*
   (``debug|info|warning|error|critical``, with ``warn``→``warning`` and
   ``exception``→``error``). ``level="ERROR"`` matches nothing.
4. **Pin ``service``, always.** promtail scrapes every container in the Compose
   project, including postgres/redis, which log plain text. ``{namespace="titan"}``
   alone runs ``| json`` against those and they land with no ``level`` label at
   all, so a level filter would silently drop them rather than "match nothing".
5. **A body filter drops lines that never had the field.** ``| pid="x"`` removes
   every line without a ``project_id``, which is most of them unless the emitter
   bound it. That is correct LogQL and looks like a bug; see the router.

RE2 has no lookahead/lookbehind and no backreferences, so "matches X but not Y"
is two chained filters, never one clever regex.
"""

import re
from dataclasses import dataclass

#: The Loki stream labels promtail actually sets (promtail-config.yaml:22-33).
#: Anything else named here would silently match nothing.
STREAM_LABELS = ("namespace", "service", "level")

#: promtail's static namespace label. Not configurable per-deployment today.
NAMESPACE = "titan"

#: What structlog can put in `level` — the log *method* name, lowercased.
#: `warn`->`warning` and `exception`->`error` happen inside structlog, so those
#: two never reach the wire. `critical` is emitted by nothing today but is a
#: real level and cheap to accept.
LEVELS = ("debug", "info", "warning", "error", "critical")

#: Body keys promoted to typed fields on `LogLine`. Everything else in the body
#: is a caller kwarg and lands in `fields`.
#:
#: The first eight are structlog's own pipeline (`logging.py`). Of the last
#: three, `project_id` and `agent_id` are bound as log context
#: (`observability/context.py`), so they arrive on every line of request- and
#: turn-scoped work. **`task_id` is not bound by anything** — `turn_context`
#: accepts one but no caller has a task id to give it, so `task_id` reaches a
#: line only where a call site passes it by hand (four places today). It is
#: promoted anyway because those four lines should still surface it typed
#: rather than bury it in `fields`.
#:
#: This set MUST stay in step with `LogLine`: a key promoted to a typed field
#: but missing here is reported twice, once typed and once inside `fields`.
#: `tests/test_logs_paging.py` asserts that structurally.
PROMOTED_KEYS = frozenset(
    {
        "ts",
        "level",
        "event",
        "module",
        "service",
        "request_id",
        "trace_id",
        "span_id",
        "project_id",
        "task_id",
        "agent_id",
    }
)


class LogQLError(ValueError):
    """A filter the builder refuses to turn into LogQL."""


@dataclass(frozen=True)
class LogFilters:
    """The viewer's filter bar, normalised. ``None``/empty means "no filter"."""

    services: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    #: Case-SENSITIVE substring, because that is what LogQL's `|=` is. Cheap:
    #: a line filter, before any parsing. (The client-side buffer filter
    #: matches this exactly — see `lib/logs/filters.ts` — so the same term
    #: returns the same lines whether it is answered from the buffer or from
    #: Loki. Lower-casing one side would quietly break that.)
    search: str | None = None
    #: RE2 regex against the raw line. No lookahead/backrefs (see module docs).
    regex: str | None = None


def escape_label_value(value: str) -> str:
    r"""Escape a value for a LogQL double-quoted string.

    Loki's lexer reads these as Go double-quoted strings, so a bare ``"`` or
    ``\`` would end the string early (or worse, smuggle in a matcher). This is
    the only thing standing between a filter value and injection into the query
    we hand Loki, so it escapes the backslash FIRST — escaping the quote first
    would double-escape the backslash it introduces.

    Control characters are escaped too, and that is not decoration: a Go
    double-quoted string may not contain a literal newline, so a ``search``
    value with one in it produces LogQL that Loki cannot parse — an opaque 400
    on a value a user could paste from a stack trace without thinking.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return "".join(_CONTROL_ESCAPES.get(ch, ch) for ch in escaped)


#: Go's escapes for the control characters that can realistically be pasted
#: into a filter box. Anything else below 0x20 goes out as \xNN.
_CONTROL_ESCAPES: dict[str, str] = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    **{chr(c): f"\\x{c:02x}" for c in range(0x20) if chr(c) not in "\n\r\t"},
    "\x7f": "\\x7f",
}


def _selector(filters: LogFilters) -> str:
    """The ``{...}`` stream selector — labels only, and always pinned."""
    parts = [f'namespace="{escape_label_value(NAMESPACE)}"']

    if filters.services:
        parts.append(_label_matcher("service", filters.services))
    if filters.levels:
        unknown = [lv for lv in filters.levels if lv not in LEVELS]
        if unknown:
            raise LogQLError(f"unknown level(s): {', '.join(sorted(unknown))}")
        parts.append(_label_matcher("level", filters.levels))

    return "{" + ", ".join(parts) + "}"


def _label_matcher(label: str, values: tuple[str, ...]) -> str:
    """``label="v"`` for one value, an anchored alternation for many.

    Label matchers are fully anchored, so the alternation needs no ``.*`` and
    each member must be regex-escaped: a service name is a plain string, not a
    pattern, and `re.escape` keeps a literal `.` from matching any character.
    """
    if len(values) == 1:
        return f'{label}="{escape_label_value(values[0])}"'
    alternation = "|".join(re.escape(v) for v in sorted(set(values)))
    return f'{label}=~"{escape_label_value(alternation)}"'


def build_query(filters: LogFilters) -> str:
    """Render ``filters`` as a LogQL stream query.

    Ordering is deliberate and is the difference between a fast query and a slow
    one: label selector, then the cheap substring line filter, then the regex
    line filter, then ``| json`` (the expensive part) and only then the body
    filters — so the parser runs over the smallest possible set of lines.
    """
    stages = [_selector(filters)]

    # Line filters run on raw bytes, before any parsing. `|=` is a plain
    # substring scan and is much cheaper than `|~`, so it goes first.
    if filters.search:
        stages.append(f'|= "{escape_label_value(filters.search)}"')
    if filters.regex:
        _validate_regex(filters.regex)
        stages.append(f'|~ "{escape_label_value(filters.regex)}"')

    body = _body_filters(filters)
    if body:
        # Name every extracted field explicitly. Besides dodging the
        # `_extracted` rename on level/service, `| json a="x", b="y"` tells Loki
        # to pull only those keys instead of exploding the whole object.
        labels = ", ".join(f'{alias}="{key}"' for alias, key, _ in body)
        stages.append(f"| json {labels}")
        stages.extend(f'| {alias} = "{escape_label_value(value)}"' for alias, _, value in body)

    return " ".join(stages)


def _body_filters(filters: LogFilters) -> list[tuple[str, str, str]]:
    """``(alias, body_key, value)`` for each JSON-body equality filter.

    The alias is what the value is bound to inside the pipeline, and it is the
    one position in the query where escaping does not apply — a LogQL label
    alias is a bare identifier, not a string. That is safe here only because
    every alias is built from this fixed list of literals: no caller-supplied
    name ever reaches it. (An earlier draft took arbitrary `fields` from the
    caller and validated the names with a regex. Nothing ever passed any, and
    the regex admitted U+017F and U+212A through `re.IGNORECASE` case-folding,
    so the extension point was dead code guarding itself badly. If a
    caller-named field is ever wanted, it needs a real allowlist, not a
    pattern.)

    The `f_` prefix keeps an alias from colliding with a stream label and
    triggering the `_extracted` rename that rule 1 exists to avoid.
    """
    pairs: list[tuple[str, str, str]] = []
    for key, value in (
        ("project_id", filters.project_id),
        ("task_id", filters.task_id),
        ("agent_id", filters.agent_id),
        ("request_id", filters.request_id),
        ("trace_id", filters.trace_id),
    ):
        if value:
            pairs.append((f"f_{key}", key, value))
    return pairs


#: Loki rejects a bad regex with a 400. Catching it here turns a confusing
#: upstream error into a field-level 422 naming the real problem.
_MAX_REGEX = 512


def _validate_regex(pattern: str) -> None:
    if len(pattern) > _MAX_REGEX:
        raise LogQLError(f"regex is too long ({len(pattern)} > {_MAX_REGEX} characters)")
    # RE2 has no lookaround and no backreferences. Python's `re` accepts both,
    # so compiling here does NOT prove Loki will: this catches only outright
    # syntax errors. The constructs RE2 lacks are rejected explicitly below.
    try:
        re.compile(pattern)
    except re.error as exc:
        raise LogQLError(f"invalid regex: {exc}") from exc
    unsupported = _re2_unsupported(pattern)
    if unsupported is not None:
        raise LogQLError(f"regex uses {unsupported}, which Loki's RE2 engine does not support")


def _re2_unsupported(pattern: str) -> str | None:
    r"""Name the first RE2-incompatible construct in ``pattern``, or None.

    A scanner rather than a list of ``re.search`` probes, because a probe
    cannot tell a construct from its own escaped text. ``a\\1`` is "a, a
    literal backslash, a 1" — perfectly valid RE2 — but a naive ``\\[1-9]``
    probe sees a backreference and rejects it. Same for ``\(?=``, which is an
    optional literal parenthesis, not a lookahead. Walking the pattern and
    consuming each escape as one unit is the only way to tell them apart.
    """
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        char = pattern[i]
        if char == "\\":
            nxt = pattern[i + 1] if i + 1 < n else ""
            # \1..\9 is a backreference; \0 is a NUL escape, which RE2 has.
            if not in_class and nxt.isdigit() and nxt != "0":
                return "a backreference"
            if nxt == "Z":
                # RE2 spells end-of-text \z; Python accepts \Z and Loki 400s.
                return r"\Z (RE2 spells end-of-text \z)"
            i += 2  # the escape and the character it escapes are one unit
            continue
        if in_class:
            in_class = char != "]"
            i += 1
            continue
        if char == "[":
            # Inside a character class `(` and `?` are literals, so no group
            # construct can start here.
            in_class = True
            i += 1
            continue
        if char == "(" and pattern.startswith("(?", i):
            rest = pattern[i + 2 :]
            for prefix, why in _GROUP_CONSTRUCTS:
                if rest.startswith(prefix):
                    return why
        i += 1
    return None


#: Longest prefix first: `(?<=` must be tested before nothing shorter shadows
#: it. `(?P<name>...)` is a named GROUP, which RE2 supports — only `(?P=name)`,
#: the named BACKreference, is missing.
_GROUP_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    ("<=", "lookbehind"),
    ("<!", "negative lookbehind"),
    ("=", "lookahead"),
    ("!", "negative lookahead"),
    ("P=", "a named backreference"),
)


def split_body(body: dict[str, object]) -> dict[str, object]:
    """The caller kwargs of a structlog line: the body minus the pipeline's own keys.

    Vol 9 §6.3 describes an ``attrs`` object holding a line's structured
    payload. structlog has no such thing — `logger.info("x", attempt=3)` puts
    `attempt` at the TOP level next to `ts` and `level`. So `attrs` is
    reconstructed here by subtraction rather than read, which is what §6.7's
    "line expansion for structured JSON payloads" needs.
    """
    return {k: v for k, v in body.items() if k not in PROMOTED_KEYS}
