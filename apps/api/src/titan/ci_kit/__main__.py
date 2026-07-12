"""``titan-ci-init`` — the one command that installs the constitution into a workspace.

This is the entry point the DevOps agent invokes through the Phase-21 terminal
tool inside its sandbox:

    titan-ci-init --package billing

The kit is baked into the sandbox image (infra/docker/titan-sandbox.Dockerfile)
and reachable on PATH, so the agent needs no network, no pip, and no knowledge of
where the kit lives — which matters, because the sandbox has neither an index nor
egress.
"""

import argparse
import sys
from pathlib import Path

from titan.ci_kit.config import KitConfig
from titan.ci_kit.install import install_kit
from titan.ci_kit.spec import ADVISORY_RULES, RULES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="titan-ci-init",
        description=(
            "Install the Engineering Standards Constitution's CI enforcement kit "
            "(Vol 6 §6) into a generated workspace."
        ),
        epilog="Gates installed: " + "; ".join(f"{rule.gate} ({rule.wfl_id})" for rule in RULES),
    )
    parser.add_argument(
        "--package",
        required=True,
        help="the Python root package holding the four layers, e.g. 'billing'",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--src", default="src", help="source root (default: src)")
    parser.add_argument(
        "--migrations", default="migrations", help="migrations dir (default: migrations)"
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="ref the coverage gate diffs against (default: origin/main)",
    )
    parser.add_argument(
        "--ts-src",
        default="",
        help="TypeScript source root; omit to disable the ESLint boundary gate",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite ruff.toml / eslint.config.mjs if the workspace already has them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = KitConfig(
        package=args.package,
        src=args.src,
        migrations=args.migrations,
        base_ref=args.base_ref,
        ts_src=args.ts_src,
    )
    result = install_kit(args.workspace, config, force=args.force)

    print(result.render())
    print("\nMachine-enforced (CI rejects a violation):")
    for rule in RULES:
        print(f"  {rule.wfl_id}  {rule.title}")
    print("\nReviewer-enforced (§6.6 — CI cannot decide these):")
    for wfl_id, title in ADVISORY_RULES:
        print(f"  {wfl_id}  {title}")
    print("\nRun the gates with:  PYTHONPATH=.titan/ci python3 -m titan.ci_kit.gates.runner")
    return 0


if __name__ == "__main__":
    sys.exit(main())
