"""TITAN-WFL-027 — the §6.2 naming table, enforced by Ruff (and ESLint for TS).

The gate runs Ruff with ``--select N`` (pep8-naming) explicitly rather than
deferring to the workspace's ``ruff.toml``. That is not redundancy: ruff.toml
selects the project's whole lint surface, so running it here would let an unused
import fail the *naming* gate. §6.5's DoD requires each fixture to fail at
exactly the gate its violation targets, and a gate that reports violations of
rules it does not own cannot satisfy that.

pep8-naming maps directly onto the §6.2 rows: N801 (PascalCase classes), N802
(snake_case functions), N803/N806 (snake_case arguments and locals), N815/N816
(no mixedCase attributes or globals).
"""

import argparse
import re
import shutil
import subprocess  # noqa: S404 — fixed argv, no shell
import sys
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, ConfigError, KitConfig, load_config
from titan.ci_kit.gates._eslint import EslintUnavailable, run_eslint
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "naming"
WFL_ID = "TITAN-WFL-027"

# `ruff check --output-format concise` -> path:line:col: CODE message
_CONCISE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\d+:\s+(?P<code>N\d+)\s+(?P<message>.*)$")

FIX = (
    "follow the §6.2 naming table (snake_case functions, PascalCase classes, "
    "SCREAMING_SNAKE constants)"
)


class GateUnavailable(Exception):
    """Ruff is not installed — the rule is unchecked, not satisfied."""


def parse_ruff(output: str) -> tuple[Finding, ...]:
    """Ruff's concise output -> findings. Pure."""
    findings: list[Finding] = []
    for line in output.splitlines():
        match = _CONCISE.match(line.strip())
        if match is None:
            continue
        findings.append(
            Finding(
                wfl_id=WFL_ID,
                path=match.group("path").replace("\\", "/"),
                line=int(match.group("line")),
                message=f"{match.group('code')} {match.group('message')}",
                fix=FIX,
            )
        )
    return tuple(findings)


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    package_root = workspace / config.package_root
    if not package_root.is_dir():
        # See layering.check: a non-existent declared package is a config typo,
        # and a skip here would read as a pass.
        raise ConfigError(
            f"[titan-ci] package root {config.package_root} does not exist in the workspace"
        )

    if shutil.which("ruff") is None:
        raise GateUnavailable("ruff is not on PATH")

    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            "ruff",
            "check",
            # --isolated ignores the workspace ruff.toml entirely. Without it,
            # `--select N` sets the rule set but ruff STILL applies the project's
            # `ignore` list and `[lint.per-file-ignores]`, so a project could add
            # `"src/**" = ["N"]` and opt out of naming enforcement — exactly the
            # kind of gate-loosening the kit exists to prevent.
            "--isolated",
            "--select",
            "N",
            "--no-fix",
            "--output-format",
            "concise",
            config.package_root.as_posix(),
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    findings = list(parse_ruff(result.stdout))
    if not findings and result.returncode not in (0, 1):
        raise GateUnavailable(f"ruff failed to run: {result.stderr.strip()}")

    # spec.py advertises this gate's mechanism as "Ruff pep8-naming (N) + ESLint
    # naming rules": when the workspace has TypeScript, the §6.2 TS rows
    # (camelCase identifiers) are enforced by ESLint's `camelcase` rule, which
    # ships in eslint.config.mjs. Fail-closed via the shared runner.
    findings.extend(_typescript_naming(workspace, config))

    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


# §6.2 rows enforced by ESLint (not by boundaries/*, which is the layering gate).
_NAMING_RULES = frozenset({"camelcase", "id-match", "@typescript-eslint/naming-convention"})


def _typescript_naming(workspace: Path, config: KitConfig) -> list[Finding]:
    ts_root = workspace / config.ts_src
    if not config.ts_src or not ts_root.is_dir():
        return []
    try:
        violations = run_eslint(workspace, config.ts_src)
    except EslintUnavailable as exc:
        raise GateUnavailable(str(exc)) from exc
    return [
        Finding(
            wfl_id=WFL_ID,
            path=v.path,
            line=v.line,
            message=f"{v.rule}: {v.message}",
            fix=FIX,
        )
        for v in violations
        if v.rule in _NAMING_RULES
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: §6.2 naming conventions")
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
