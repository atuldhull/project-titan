"""TITAN-WFL-022 — Clean Architecture layering, dependencies inward only.

§6.1 names the mechanism, so the kit uses it rather than reimplementing it:
import-linter for Python (a ``layers`` contract over the four §6.1 layers) and
eslint-plugin-boundaries for TypeScript. The kit ships both configs; this gate
runs them and normalises their output into :class:`GateOutcome`.

The acceptance test in §6.1 is "an import from ``domain`` into ``infrastructure``
fails CI" — that is exactly a layers-contract violation, because the config lists
the layers outermost-first and a lower layer reaching up is what the contract
forbids.
"""

import argparse
import os
import shutil
import subprocess  # noqa: S404 — fixed argv, no shell
import sys
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, ConfigError, KitConfig, load_config
from titan.ci_kit.gates._eslint import EslintUnavailable, run_eslint
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "layering"
WFL_ID = "TITAN-WFL-022"

IMPORTLINTER_CONFIG = ".importlinter"

FIX = (
    "dependencies point inward only (interface -> infrastructure -> application -> "
    "domain); invert the dependency behind a port declared in the inner layer "
    "(§6.1 TITAN-WFL-022)"
)


class GateUnavailable(Exception):
    """The tool this gate delegates to is not installed.

    Raised rather than returned as a pass: a workspace where ``lint-imports`` is
    missing has NOT satisfied TITAN-WFL-022, it has failed to check it, and the
    runner must say so.
    """


def _run(
    argv: list[str], cwd: Path, env_path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_path:
        # import-linter resolves the root package through the import system, so
        # a src-layout workspace needs its source root on the path.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{env_path}{os.pathsep}{existing}" if existing else env_path
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        # Decode tool output as UTF-8 regardless of host locale: import-linter
        # prints box-drawing characters that crash the default cp1252 reader on
        # Windows. errors="replace" so a stray byte never corrupts a finding.
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
        env=env,
    )


def _python_layering(workspace: Path, config: KitConfig) -> tuple[Finding, ...]:
    if shutil.which("lint-imports") is None:
        raise GateUnavailable("lint-imports (import-linter) is not on PATH")

    config_path = workspace / IMPORTLINTER_CONFIG
    if not config_path.is_file():
        raise ConfigError(f"no {IMPORTLINTER_CONFIG} in the workspace — run `titan-ci-init`")

    result = _run(
        ["lint-imports", "--config", IMPORTLINTER_CONFIG],
        cwd=workspace,
        env_path=str((workspace / config.src).resolve()),
    )
    if result.returncode == 0:
        return ()

    output = f"{result.stdout}\n{result.stderr}".strip()
    # import-linter prints each broken import as `pkg.a -> pkg.b (l.12)`. Keep the
    # tool's own rendering rather than re-deriving it — it is the citation.
    violations = [
        line.strip()
        for line in output.splitlines()
        if "->" in line and not line.strip().startswith("-")
    ]
    if not violations:
        violations = [output or "import-linter reported a broken contract"]

    return tuple(
        Finding(
            wfl_id=WFL_ID,
            path=IMPORTLINTER_CONFIG,
            line=0,
            message=violation,
            fix=FIX,
        )
        for violation in violations
    )


def _typescript_layering(workspace: Path, config: KitConfig) -> tuple[Finding, ...]:
    ts_root = workspace / config.ts_src
    if not config.ts_src or not ts_root.is_dir():
        return ()
    try:
        violations = run_eslint(workspace, config.ts_src)
    except EslintUnavailable as exc:
        # A TS workspace that cannot run ESLint has NOT satisfied TITAN-WFL-022.
        raise GateUnavailable(str(exc)) from exc
    # Only the layering rule (boundaries/*) is this gate's concern; camelcase etc.
    # belong to the naming gate, which runs ESLint for its own rules.
    return tuple(
        Finding(
            wfl_id=WFL_ID,
            path=v.path,
            line=v.line,
            message=f"{v.rule}: {v.message}",
            fix=FIX,
        )
        for v in violations
        if v.rule.startswith("boundaries/") or v.rule == "boundaries"
    )


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    package_root = workspace / config.package_root
    if not package_root.is_dir():
        # NOT a skip. A declared package that does not exist is a typo in
        # .titan-ci.toml, and skipping would let one wrong character silently
        # disable the layering, naming and ORM gates at once.
        raise ConfigError(
            f"[titan-ci] package root {config.package_root} does not exist in the workspace"
        )

    findings = _python_layering(workspace, config) + _typescript_layering(workspace, config)
    if findings:
        return failed(GATE, WFL_ID, findings)
    return passed(GATE, WFL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: Clean Architecture layering")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=f"workspace root (must contain {CONFIG_FILENAME})",
    )
    args = parser.parse_args(argv)
    try:
        outcome = check(args.workspace)
    except (GateUnavailable, ConfigError) as exc:
        print(f"ERROR {GATE} ({WFL_ID}): {exc}")
        return 2
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
