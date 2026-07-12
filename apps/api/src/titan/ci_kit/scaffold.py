"""Render the Phase-46 project templates into a workspace.

The templates under ``templates/project/`` are the constitution-compliant starter
a generated Python service is built on: the §6.3 error taxonomy + RFC 9457
translation, the §6.4 structured-logging pipeline with redaction, guarded
external I/O, and the §6.5 test pyramid (unit + testcontainers integration + e2e
+ k6 slots) with injected clocks and seeds.

Templates are stored as ``*.py.tmpl`` (and one ``pyproject.toml.tmpl``) carrying a
single ``__PKG__`` sentinel — the root package name — in both their paths and
their bodies. Storing them as ``.tmpl`` (not ``.py``) keeps titan's own ruff/mypy
from trying to type-check a file that imports ``__PKG__``; the scaffold's own
test renders them and runs the full Phase-45 kit to prove they are compliant.

Rendering is pure (returns KitFiles); the writing half is install.install_kit.
"""

from pathlib import Path

from titan.ci_kit.render import KitFile

PACKAGE_SENTINEL = "__PKG__"
_PROJECT_TEMPLATES = Path(__file__).parent / "templates" / "project"


class ScaffoldUnavailable(Exception):
    """The project templates are not present in this copy of the kit.

    render._kit_sources vendors the kit's ``*.py`` into a workspace's
    ``.titan/ci`` but NOT the ``templates/project`` tree, so running
    ``python -m titan.ci_kit --scaffold`` from a vendored copy would otherwise
    render nothing and falsely report success. Scaffolding is meant to run from
    the full titan install or the sandbox image (which carry the templates)."""


def _render_path(relative: str, package: str) -> str:
    # Substitute the package into the path and drop the .tmpl marker.
    rendered = relative.replace(PACKAGE_SENTINEL, package)
    return rendered[: -len(".tmpl")] if rendered.endswith(".tmpl") else rendered


def scaffold_files(package: str) -> tuple[KitFile, ...]:
    """Every project-template file, rendered for ``package``. Pure.

    Raises ScaffoldUnavailable if the template tree is absent (a vendored kit
    copy) rather than silently rendering nothing."""
    if not _PROJECT_TEMPLATES.is_dir():
        raise ScaffoldUnavailable(
            "the project templates are not available in this kit copy; "
            "scaffold from the full titan install or the sandbox image"
        )
    files: list[KitFile] = []
    for source in sorted(_PROJECT_TEMPLATES.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(_PROJECT_TEMPLATES).as_posix()
        content = source.read_text(encoding="utf-8").replace(PACKAGE_SENTINEL, package)
        files.append(KitFile(path=_render_path(relative, package), content=content))
    if not files:
        raise ScaffoldUnavailable("the project template tree is empty")
    return tuple(files)


__all__ = ["PACKAGE_SENTINEL", "ScaffoldUnavailable", "scaffold_files"]
