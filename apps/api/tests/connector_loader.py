"""Load a first-party connector's modules by path for in-process testing (Phase 54).

Connectors live at ``<repo>/plugins/first_party/<name>/`` and are NOT on the core Python
path — they run in their own venv as subprocesses in production. These tests import their
STDLIB-ONLY modules directly to exercise the connector logic in-process against fake
transports (TITAN-INT-040 "recorded fixtures when offline").

Crucially, ``load_connector`` calls ``pytest.skip`` when the connector directory is ABSENT,
so deleting a connector's directory leaves the core suite green — the TITAN-INT-011
acceptance ("deleting any connector's directory leaves core tests green")."""

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_FIRST_PARTY = Path(__file__).parents[3] / "plugins" / "first_party"


def connector_dir(name: str) -> Path:
    return _FIRST_PARTY / name


def load_connector(name: str, module: str) -> ModuleType:
    """Import ``module`` from connector ``name``'s directory, or skip if the connector is
    not installed. The directory is put on ``sys.path`` (connectors use flat, connector-
    prefixed module names, so there is no cross-connector collision)."""
    path = connector_dir(name)
    if not (path / f"{module}.py").exists():
        # allow_module_level: these loaders run at import scope, so a bare skip would be a
        # COLLECTION ERROR (red suite) — the opposite of the INT-011 acceptance. This skips
        # the whole module cleanly when the connector directory has been deleted.
        pytest.skip(
            f"connector '{name}' is not installed (module {module}.py absent)",
            allow_module_level=True,
        )
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return importlib.import_module(module)
