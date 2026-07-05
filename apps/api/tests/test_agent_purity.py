"""TITAN-INT-001 agent-purity lint self-tests (Phase 18 DoD: the CI lint fails
on a planted subprocess call under titan/agents/**)."""

from pathlib import Path

from scripts.check_agent_purity import find_violations


def _plant(tmp_path: Path, name: str, source: str) -> Path:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def test_planted_subprocess_call_fails_the_lint(tmp_path: Path) -> None:
    # The DoD probe: a subprocess call smuggled into agent code.
    _plant(
        tmp_path,
        "runner.py",
        "import subprocess\n\n\ndef run() -> None:\n    subprocess.run(['rm', '-rf', '/'])\n",
    )
    violations = find_violations(tmp_path)
    assert violations, "planted subprocess call was not caught"
    assert any("subprocess" in v.message for v in violations)
    assert all("TITAN-INT-001" in v.message for v in violations)


def test_open_write_modes_are_caught_and_reads_pass(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "files.py",
        "def f(p: str, m: str) -> None:\n"
        "    open(p, 'w')\n"
        "    open(p, mode='a+')\n"
        "    open(p, m)\n"  # non-literal mode: cannot prove read-only
        "    open(p)\n"  # plain read — allowed
        "    open(p, 'rb')\n",  # binary read — allowed
    )
    violations = find_violations(tmp_path)
    assert len(violations) == 3
    assert sum("write mode" in v.message for v in violations) == 2
    assert sum("non-literal mode" in v.message for v in violations) == 1


def test_raw_http_clients_sockets_and_pathlib_writes_are_caught(tmp_path: Path) -> None:
    _plant(tmp_path, "net1.py", "import httpx\n")
    _plant(tmp_path, "net2.py", "from urllib import request\n")
    _plant(tmp_path, "net3.py", "import http.client\n")
    _plant(tmp_path, "net4.py", "import socket\n")
    _plant(tmp_path, "spawn.py", "from asyncio import create_subprocess_exec\n")
    _plant(
        tmp_path,
        "spawn2.py",
        "import asyncio\n\n\nasync def f() -> None:\n"
        "    await asyncio.create_subprocess_shell('ls')\n",
    )
    _plant(tmp_path, "spawn3.py", "import os\n\n\ndef f() -> None:\n    os.system('ls')\n")
    _plant(
        tmp_path,
        "write.py",
        "from pathlib import Path\n\n\ndef f() -> None:\n    Path('x').write_text('data')\n",
    )
    violations = find_violations(tmp_path)
    files = {v.path.name for v in violations}
    assert files == {
        "net1.py",
        "net2.py",
        "net3.py",
        "net4.py",
        "spawn.py",
        "spawn2.py",
        "spawn3.py",
        "write.py",
    }


def test_os_spawner_from_imports_and_aliases_are_caught(tmp_path: Path) -> None:
    # `from os import system` binds a bare-name spawner — flagged at the import
    # site; `import os as o` must not launder the attribute form either.
    _plant(
        tmp_path, "bare.py", "from os import system\n\n\ndef f(c: str) -> None:\n    system(c)\n"
    )
    _plant(
        tmp_path,
        "aliased.py",
        "import os as operating\n\n\ndef f() -> None:\n    operating.system('ls')\n",
    )
    _plant(tmp_path, "renamed.py", "from os import execv as run_it\n")
    violations = find_violations(tmp_path)
    files = {v.path.name for v in violations}
    assert files == {"bare.py", "aliased.py", "renamed.py"}
    # os.path / os.environ usage stays legal.
    _plant(tmp_path, "bare.py", "import os\n\nHOME = os.environ.get('HOME')\n")
    _plant(tmp_path, "aliased.py", "import os\n\nJOIN = os.path.join('a', 'b')\n")
    _plant(tmp_path, "renamed.py", "import os\n")
    assert find_violations(tmp_path) == []


def test_clean_agent_code_passes(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "clean.py",
        "import json\nfrom titan.tools.service import ToolBus\n\n\n"
        "def read(p: str) -> str:\n    with open(p) as fh:\n        return fh.read()\n",
    )
    assert find_violations(tmp_path) == []


def test_real_agents_tree_is_pure() -> None:
    # The actual TITAN-INT-001 gate: titan/agents/** is side-effect free today
    # and must stay that way.
    root = Path("src/titan/agents")
    assert root.exists()
    assert find_violations(root) == []
