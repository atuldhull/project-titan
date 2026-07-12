"""TITAN-WFL-031 — deterministic, hermetic tests.

§6.5 requires tests to be hermetic: no network calls to real external services,
no time/random flakiness (injected clocks and seeds), parallel-safe. A flaky test
is a P1 defect. This gate is the machine half: it AST-scans the test tree for the
two failure modes the spec names — a real external call, or wall-clock /
unseeded-random nondeterminism — and fails the build before the flake ships.

Scope and precision. The gate scans only the configured test directory, and it
flags KNOWN non-hermetic constructs rather than trying to prove hermeticity
(undecidable). It deliberately does NOT flag ``uuid.uuid4()`` (ubiquitous for
unique ids and rarely the cause of an assertion flake) or connections to
localhost/127.0.0.1 (a testcontainers integration test legitimately talks to a
container). Over-flagging would get the gate switched off, which enforces
nothing.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, load_config
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed, skipped

GATE = "hermeticity"
WFL_ID = "TITAN-WFL-031"

# Real-network entry points, as fully-qualified ``module.attr`` names. httpx and
# aiohttp are the modern clients (httpx is even a scaffold dependency), so they
# are here alongside requests/urllib/socket. Localhost/container hosts are fine —
# only real external hosts are flagged, judged from a call's URL argument below.
_NETWORK_CALLS: frozenset[str] = frozenset(
    {
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "requests.head",
        "requests.request",
        "requests.Session",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.request",
        "httpx.stream",
        "aiohttp.request",
        "aiohttp.ClientSession",
        "urllib.request.urlopen",
        "socket.socket",
        "socket.create_connection",
        "smtplib.SMTP",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
    }
)

# Wall-clock / unseeded-random calls. Tests must inject a clock and seed. uuid4 is
# intentionally excluded (see module docstring).
_NONDETERMINISM: frozenset[str] = frozenset(
    {
        "random.random",
        "random.randint",
        "random.randrange",
        "random.choice",
        "random.choices",
        "random.shuffle",
        "random.uniform",
        "random.sample",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "time.time",
        "time.monotonic",
        "uuid.uuid1",  # time+MAC based, non-deterministic AND deanonymizing
    }
)

# A non-local http(s) URL literal in a test = a real external call. The trailing
# [\w.-] requires an actual host character, so an f-string's constant prefix
# ("https://" before the interpolated {host}) is NOT flagged. The local-host
# exclusions are anchored to a host boundary ([:/ ]|$) so a lookalike like
# https://localhost.evil.com does NOT slip through the localhost exclusion.
_EXTERNAL_URL = re.compile(
    r"https?://"
    r"(?!(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|example\.(?:com|org|net))(?:[:/]|$))"
    r"[\w.\-]",
    re.IGNORECASE,
)

FIX_NET = (
    "use a fake/contract stub or a testcontainer, not a real external service (§6.5 TITAN-WFL-031)"
)
FIX_RNG = (
    "inject a seeded RNG / a fixed clock fixture instead of wall-clock time or "
    "unseeded randomness (§6.5 TITAN-WFL-031)"
)


def _build_aliases(tree: ast.Module) -> dict[str, str]:
    """Map each imported local name to its fully-qualified module path.

    Resolving imports is what lets the gate see through every call form:
    ``import requests as r`` -> {r: requests}; ``from random import random`` ->
    {random: random.random}; ``from datetime import datetime as dt`` ->
    {dt: datetime.datetime}. Without this the matcher only caught the single
    ``import module; module.fn()`` idiom and missed the rest.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.asname:
                    aliases[name.asname] = name.name
                else:
                    # `import a.b.c` binds `a`; the full path is reached via a.b.c.
                    top = name.name.split(".", 1)[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    return aliases


def _resolved_name(func: ast.expr, aliases: dict[str, str]) -> str:
    """The call target as a fully-qualified dotted name, resolving the head alias.

    ``r.get`` with {r: requests} -> ``requests.get``; a bare ``random()`` with
    {random: random.random} -> ``random.random``; ``datetime.now`` with
    {datetime: datetime.datetime} -> ``datetime.datetime.now`` (whose ``.now``
    suffix matches the ``datetime.now`` pattern)."""
    attrs: list[str] = []
    cur: ast.expr = func
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        head = aliases.get(cur.id, cur.id)
        return ".".join([head, *reversed(attrs)])
    return ".".join(reversed(attrs))


def _suffix_in(dotted: str, patterns: frozenset[str]) -> bool:
    """True if ``dotted`` equals a pattern or ends with ``.<pattern>`` (matching
    on a dotted boundary so a bare local name can't collide)."""
    return any(dotted == pattern or dotted.endswith("." + pattern) for pattern in patterns)


def scan_source(source: str, path: str) -> list[Finding]:
    """Every hermeticity violation in one test module. Pure.

    A file that does not parse is not this gate's problem (ruff/mypy own syntax
    errors); return nothing rather than guess.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    aliases = _build_aliases(tree)
    findings: list[Finding] = []

    def add(line: int, message: str, fix: str) -> None:
        findings.append(Finding(wfl_id=WFL_ID, path=path, line=line, message=message, fix=fix))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _resolved_name(node.func, aliases)
            if _suffix_in(name, _NETWORK_CALLS):
                add(node.lineno, f"real network call {name!r} in a test", FIX_NET)
            elif _suffix_in(name, _NONDETERMINISM):
                add(node.lineno, f"nondeterministic call {name!r} in a test", FIX_RNG)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _EXTERNAL_URL.search(node.value):
                add(
                    node.lineno,
                    f"external URL literal {node.value[:60]!r} in a test",
                    FIX_NET,
                )

    return findings


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    test_root = workspace / config.tests
    if not test_root.is_dir():
        return skipped(GATE, WFL_ID, f"no test directory at {config.tests}")

    findings: list[Finding] = []
    paths = sorted(test_root.rglob("*.py"))
    for path in paths:
        findings.extend(
            scan_source(path.read_text(encoding="utf-8"), path.relative_to(workspace).as_posix())
        )

    if not paths:
        return skipped(GATE, WFL_ID, f"no Python tests under {config.tests}")
    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: hermetic, deterministic tests")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=f"workspace root (must contain {CONFIG_FILENAME})",
    )
    args = parser.parse_args(argv)
    outcome = check(args.workspace)
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
