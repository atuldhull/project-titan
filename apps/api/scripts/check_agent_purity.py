"""CI lint: agent code may not side-effect outside the Tool Bus (TITAN-INT-001).

Every externally visible side-effect performed by an agent MUST pass through
the Tool Bus. This AST lint rejects, anywhere under ``src/titan/agents/``:

  * ``subprocess`` (and ``os.system``/``os.popen``/``os.exec*``/``os.spawn*``,
    ``asyncio.create_subprocess_*``) — no process spawning
  * ``open()`` in write mode (and ``Path.write_text``/``write_bytes``,
    ``Path.open`` with a write mode, or any non-literal mode) — no direct
    file writes
  * raw HTTP clients / sockets (httpx, aiohttp, requests, urllib3,
    urllib.request, http.client, socket) — no direct network egress

Usage: uv run python scripts/check_agent_purity.py [root]
The default root is ``src/titan/agents``; tests point it at fixture trees.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_BANNED_MODULES = {
    "subprocess": "process spawning must go through the Tool Bus",
    "httpx": "raw HTTP clients are forbidden in agent code",
    "aiohttp": "raw HTTP clients are forbidden in agent code",
    "requests": "raw HTTP clients are forbidden in agent code",
    "urllib3": "raw HTTP clients are forbidden in agent code",
    "urllib.request": "raw HTTP clients are forbidden in agent code",
    "http.client": "raw HTTP clients are forbidden in agent code",
    "socket": "raw sockets are forbidden in agent code",
    "pty": "process spawning must go through the Tool Bus",
}

_WRITE_MODE_CHARS = set("wax+")
_OS_SPAWNERS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _banned_module(name: str) -> str | None:
    """The ban reason if ``name`` or any of its parents is banned."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        reason = _BANNED_MODULES.get(".".join(parts[:i]))
        if reason is not None:
            return reason
    return None


def _is_os_spawner(name: str) -> bool:
    return name in _OS_SPAWNERS or name.startswith(("exec", "spawn"))


class _PurityVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[Violation] = []
        # Names the module binds to the os module (`import os`, `import os as o`)
        # so aliased attribute spawns (o.system) are still caught.
        self.os_aliases: set[str] = set()

    def _flag(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        self.violations.append(Violation(self.path, line, f"{message} (TITAN-INT-001)"))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            reason = _banned_module(alias.name)
            if reason is not None:
                self._flag(node, f"import of {alias.name!r}: {reason}")
            if alias.name == "os":
                self.os_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            candidate = f"{module}.{alias.name}" if module else alias.name
            reason = _banned_module(module) or _banned_module(candidate)
            if reason is None and alias.name.startswith("create_subprocess"):
                reason = "process spawning must go through the Tool Bus"
            if reason is None and module == "os" and _is_os_spawner(alias.name):
                # `from os import system` (with or without an alias) — flagged
                # at the import site, receiver-independent.
                reason = "process spawning must go through the Tool Bus"
            if reason is not None:
                self._flag(node, f"import of {candidate!r}: {reason}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            self._check_open_mode(node, "open()")
        elif isinstance(func, ast.Attribute):
            if func.attr in ("write_text", "write_bytes"):
                self._flag(node, f".{func.attr}() writes a file directly")
            elif func.attr == "open":
                self._check_open_mode(node, ".open()")
            elif func.attr.startswith("create_subprocess"):
                self._flag(node, f".{func.attr}() spawns a process")
            elif (
                isinstance(func.value, ast.Name)
                and func.value.id in (self.os_aliases or {"os"})
                and _is_os_spawner(func.attr)
            ):
                self._flag(node, f"os.{func.attr}() spawns a process")
        self.generic_visit(node)

    def _check_open_mode(self, node: ast.Call, label: str) -> None:
        mode: ast.expr | None = None
        if len(node.args) >= 2:
            mode = node.args[1]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if mode is None:
            return  # default 'r' — reads are fine
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            if _WRITE_MODE_CHARS & set(mode.value):
                self._flag(node, f"{label} in write mode {mode.value!r}")
        else:
            self._flag(node, f"{label} with a non-literal mode (cannot prove read-only)")


def find_violations(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for source in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except SyntaxError as exc:
            violations.append(Violation(source, exc.lineno or 0, f"could not parse: {exc.msg}"))
            continue
        visitor = _PurityVisitor(source)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("src/titan/agents")
    if not root.exists():
        sys.stderr.write(f"agent-purity check: root {root} does not exist\n")
        raise SystemExit(2)
    violations = find_violations(root)
    if violations:
        for violation in violations:
            sys.stderr.write(violation.render() + "\n")
        sys.stderr.write(f"agent-purity check FAILED: {len(violations)} violation(s)\n")
        raise SystemExit(1)
    sys.stdout.write(f"agent-purity check OK: {root} is free of direct side-effects\n")


if __name__ == "__main__":
    main()
