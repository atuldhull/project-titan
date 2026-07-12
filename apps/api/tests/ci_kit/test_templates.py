"""Phase 46 DoD: the generated-workspace templates.

Rendering the templates into a fresh workspace must produce a project that passes
the full Phase-45 CI kit (including the templates' own error/logging/test suites);
a hermeticity violation must fail the template test gate. Both halves are checked
here against the SAME code CI runs.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci_kit.conftest import git
from titan.ci_kit.config import KitConfig
from titan.ci_kit.gates import hermeticity, log_scan
from titan.ci_kit.gates.runner import run_gates
from titan.ci_kit.install import install_kit
from titan.ci_kit.scaffold import scaffold_files

PACKAGE = "billing"


def _scaffold_repo(tmp_path: Path) -> Path:
    """A git workspace scaffolded from the templates, on a feature branch with a
    covered change + coverage.xml so every gate has something to judge."""
    install_kit(
        tmp_path,
        KitConfig(package=PACKAGE, src="src", base_ref="main"),
        scaffold=True,
    )
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@titan.local")
    git(tmp_path, "config", "user.name", "titan-ci")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--no-verify", "-m", "feat(billing): scaffold from titan templates")

    git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "src" / PACKAGE / "application" / "fees.py").write_text(
        "def surcharge(cents: int) -> int:\n    return cents + 10\n",
        encoding="utf-8",
        newline="\n",
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "feat(billing): add the fees helper")
    # A coverage.xml in the REAL coverage.py shape: filenames are relative to a
    # <source> root, NOT full-from-root. This is what `pytest --cov` emits with
    # the scaffold's shipped [tool.coverage.run] source setting, and it is the
    # exact shape that must round-trip through the diff-coverage gate.
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="0.9" branch-rate="0.9">\n'
        f"  <sources><source>src/{PACKAGE}</source></sources>\n"
        "  <packages><package><classes>"
        '<class filename="application/fees.py"><lines>'
        '<line number="1" hits="1"/><line number="2" hits="1"/>'
        "</lines></class></classes></package></packages></coverage>\n",
        encoding="utf-8",
        newline="\n",
    )
    return tmp_path


def _run(cmd: list[str], cwd: Path, extra_path: str = "") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_path:
        env["PYTHONPATH"] = extra_path
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )


def test_scaffold_renders_a_complete_project(tmp_path: Path) -> None:
    paths = {f.path for f in scaffold_files(PACKAGE)}
    for expected in (
        f"src/{PACKAGE}/domain/errors.py",
        f"src/{PACKAGE}/interface/problem_details.py",
        f"src/{PACKAGE}/infrastructure/external_io.py",
        f"src/{PACKAGE}/observability/logging.py",
        "tests/conftest.py",
        "tests/test_errors.py",
        "tests/test_logging.py",
        "tests/test_external_io.py",
        "pyproject.toml",
        "perf/smoke.js",
    ):
        assert expected in paths, f"scaffold is missing {expected}"
    # The sentinel must be fully substituted.
    assert not any("__PKG__" in f.content for f in scaffold_files(PACKAGE))


def test_scaffolded_workspace_passes_the_full_kit(tmp_path: Path) -> None:
    """DoD part 1: the templates pass every Phase-45 gate unmodified."""
    repo = _scaffold_repo(tmp_path)
    reports = {r.gate: r for r in run_gates(repo)}
    failing = [r.render() for r in reports.values() if not r.ok]
    assert not failing, "the scaffold must pass every gate:\n" + "\n".join(failing)


def test_templates_own_suites_pass(tmp_path: Path) -> None:
    """DoD part 1 (cont.): the templates' own error/logging/IO suites pass."""
    repo = _scaffold_repo(tmp_path)
    result = _run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-cov", "-o", "addopts="],
        repo,
        extra_path=str(repo / "src"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_scaffold_passes_diff_coverage_under_real_coverage_py(tmp_path: Path) -> None:
    """DoD (honest): render the scaffold, run the REAL coverage.py the shipped
    pyproject configures, and prove the diff-coverage gate passes on it.

    This is the test the fabricated coverage.xml could not be: coverage.py emits
    filenames RELATIVE to the [tool.coverage.run] source root, and the gate must
    reconstruct the full path to match the git diff. The whole scaffold is the
    diff (feature branch off an empty main), and every source file it ships is
    covered by the templates' own tests."""
    from titan.ci_kit.gates import diff_coverage

    # Empty main, then the scaffold lands whole on a feature branch.
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@titan.local")
    git(tmp_path, "config", "user.name", "titan-ci")
    git(tmp_path, "commit", "--allow-empty", "-m", "chore: empty root")
    git(tmp_path, "checkout", "-b", "feature")
    install_kit(tmp_path, KitConfig(package=PACKAGE, src="src", base_ref="main"), scaffold=True)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--no-verify", "-m", "feat(billing): scaffold the service")

    # The exact CI command: bare --cov reads the scaffold's [tool.coverage.run].
    result = _run(
        [sys.executable, "-m", "pytest", "tests/", "--cov", "--cov-branch", "--cov-report=xml"],
        tmp_path,
        extra_path=str(tmp_path / "src"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "coverage.xml").is_file(), "pytest --cov must emit coverage.xml"

    outcome = diff_coverage.check(tmp_path)
    assert outcome.ok, "the scaffold must pass diff_coverage on real coverage.py output:\n" + (
        outcome.render()
    )


def test_hermeticity_violation_fails_the_template_gate(tmp_path: Path) -> None:
    """DoD part 2: a test hitting a real URL / using unseeded randomness fails."""
    repo = _scaffold_repo(tmp_path)
    (repo / "tests" / "test_leaky.py").write_text(
        "import requests\n\n\ndef test_calls_out() -> None:\n"
        '    requests.get("https://api.stripe.com/charges")\n',
        encoding="utf-8",
        newline="\n",
    )
    outcome = hermeticity.check(repo)
    assert not outcome.ok
    assert any("real network call" in f.message for f in outcome.findings)


def test_unseeded_randomness_fails_the_template_gate(tmp_path: Path) -> None:
    repo = _scaffold_repo(tmp_path)
    (repo / "tests" / "test_flaky.py").write_text(
        "import random\n\n\ndef test_random() -> None:\n    assert random.random() < 1\n",
        encoding="utf-8",
        newline="\n",
    )
    outcome = hermeticity.check(repo)
    assert not outcome.ok
    assert any("nondeterministic" in f.message for f in outcome.findings)


def test_logging_a_secret_fails_the_log_scan_gate(tmp_path: Path) -> None:
    repo = _scaffold_repo(tmp_path)
    (repo / "src" / PACKAGE / "application" / "leaky.py").write_text(
        "from billing.observability.logging import get_logger\n\n"
        "_log = get_logger(__name__)\n\n\n"
        "def sign_in(password: str) -> None:\n"
        '    _log.info("sign_in", password=password)\n',
        encoding="utf-8",
        newline="\n",
    )
    outcome = log_scan.check(repo)
    assert not outcome.ok
    assert any("sensitive field" in f.message for f in outcome.findings)


def test_scaffold_fails_loudly_when_templates_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendored .titan/ci copy has no templates/project tree; --scaffold there
    must raise, not silently render nothing and report success."""
    from titan.ci_kit import scaffold

    monkeypatch.setattr(scaffold, "_PROJECT_TEMPLATES", Path("/no/such/templates"))
    with pytest.raises(scaffold.ScaffoldUnavailable):
        scaffold.scaffold_files("billing")


def test_reinstall_preserves_edited_scaffold_sources(tmp_path: Path) -> None:
    """Re-running the installer to pull new gates must not clobber project code
    the team has since edited."""
    config = KitConfig(package=PACKAGE, src="src", base_ref="main")
    install_kit(tmp_path, config, scaffold=True)
    errors = tmp_path / "src" / PACKAGE / "domain" / "errors.py"
    errors.write_text("# the team's own edits\n", encoding="utf-8")

    result = install_kit(tmp_path, config, scaffold=True)
    assert f"src/{PACKAGE}/domain/errors.py" in result.skipped
    assert errors.read_text(encoding="utf-8") == "# the team's own edits\n"
