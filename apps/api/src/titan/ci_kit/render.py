"""Render the kit's files for a workspace. Pure: no filesystem writes.

Splitting render from install is what makes the kit testable without a temp dir
and, more usefully, what lets ``titan-ci-init --check`` tell a workspace that its
config has drifted from what the current kit would produce — a workspace that
hand-edited its ``.importlinter`` to delete a layer has opted out of
TITAN-WFL-022, and CI should say so.
"""

from dataclasses import dataclass
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, LAYERS, KitConfig

# Where the kit installs its own source inside the workspace. The path mirrors
# the import path (`titan/ci_kit/...`) so that `PYTHONPATH=.titan/ci` makes
# `python -m titan.ci_kit.gates.runner` resolve — the SAME module path the repo
# and the sandbox image use. One mechanism, three places, no drift.
KIT_ROOT = ".titan/ci"

TEMPLATES = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class KitFile:
    """One file the kit drops into a workspace."""

    path: str
    """Workspace-relative, POSIX-separated."""

    content: str
    executable: bool = False


def _titan_ci_toml(config: KitConfig) -> str:
    ts_line = f'ts_src = "{config.ts_src}"\n' if config.ts_src else ""
    return (
        "# The Engineering Standards Constitution — Vol 6 §6 (Phase 43 artifact,\n"
        "# Phase 45 enforcement). Written by `titan-ci-init`.\n"
        "#\n"
        "# This file says only WHERE this workspace keeps its code. The rules\n"
        "# themselves — the four layers, the >=80%/>=70% coverage floors — are\n"
        "# fixed by the constitution and are deliberately not configurable here.\n"
        "\n"
        "[titan-ci]\n"
        f'package = "{config.package}"\n'
        f'src = "{config.src}"\n'
        f'migrations = "{config.migrations}"\n'
        f'base_ref = "{config.base_ref}"\n'
        f"{ts_line}"
    )


def _importlinter(config: KitConfig) -> str:
    # Layers are declared optional (import-linter's parenthesis syntax): a
    # service with no interface layer yet must still be LINTED, not error out
    # with "module not found". A missing layer cannot hide a violation — there is
    # nothing in it to violate.
    layers = "\n".join(f"    ({layer})" for layer in LAYERS)
    return (
        "# TITAN-WFL-022 — Clean Architecture layering (Vol 6 §6.1).\n"
        "# Written by `titan-ci-init`. Layers are listed OUTERMOST FIRST: a layer\n"
        "# may import the layers below it and never one above. An import from\n"
        "# domain into infrastructure is therefore a broken contract, which is\n"
        "# exactly the acceptance test §6.1 states.\n"
        "\n"
        "[importlinter]\n"
        f"root_package = {config.package}\n"
        "\n"
        "[importlinter:contract:clean-architecture]\n"
        "name = TITAN-WFL-022: Clean Architecture layering (dependencies point inward only)\n"
        "type = layers\n"
        "layers =\n"
        f"{layers}\n"
        "containers =\n"
        f"    {config.package}\n"
    )


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _kit_sources() -> list[KitFile]:
    """The kit's own source, vendored into the workspace at ``.titan/ci``.

    Vendoring rather than pip-installing is deliberate: the generated project's
    CI must be able to run the gates with nothing but a Python interpreter, and
    the DevOps agent installs the kit inside a network-denied sandbox where
    `pip install` cannot reach an index.
    """
    package_dir = Path(__file__).parent  # .../titan/ci_kit
    titan_dir = package_dir.parent  # .../titan

    files = [
        KitFile(
            path=f"{KIT_ROOT}/titan/__init__.py",
            content=(titan_dir / "__init__.py").read_text(encoding="utf-8"),
        )
    ]
    for source in sorted(package_dir.rglob("*.py")):
        relative = source.relative_to(titan_dir).as_posix()
        files.append(
            KitFile(
                path=f"{KIT_ROOT}/titan/{relative}",
                content=source.read_text(encoding="utf-8"),
            )
        )
    # The templates travel with the kit so the workspace copy can re-init itself.
    for template in sorted(TEMPLATES.iterdir()):
        if template.is_file():
            relative = template.relative_to(titan_dir).as_posix()
            files.append(
                KitFile(
                    path=f"{KIT_ROOT}/titan/{relative}",
                    content=template.read_text(encoding="utf-8"),
                )
            )
    return files


def render_kit(config: KitConfig) -> tuple[KitFile, ...]:
    """Every file `titan-ci-init` writes, for this workspace."""
    files = [
        KitFile(path=CONFIG_FILENAME, content=_titan_ci_toml(config)),
        KitFile(path=".importlinter", content=_importlinter(config)),
        KitFile(path="ruff.toml", content=_template("ruff.toml")),
        KitFile(path="eslint.config.mjs", content=_template("eslint.config.mjs")),
        KitFile(
            path=".githooks/commit-msg",
            content=_template("commit-msg"),
            executable=True,
        ),
        KitFile(
            path=".github/workflows/constitution.yml",
            content=_template("constitution.yml"),
        ),
    ]
    files.extend(_kit_sources())
    return tuple(files)


__all__ = ["KIT_ROOT", "KitFile", "render_kit"]
