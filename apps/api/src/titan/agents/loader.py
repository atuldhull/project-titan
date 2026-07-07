"""Load + validate the agent roster from YAML at startup (TITAN-AGT-001/006/008).

The roster is *configuration*: one ``<agent_id>.yaml`` per role under the agents
config directory. This loader turns that directory into a validated
:class:`~titan.agents.registry.AgentRegistry`, and it is deliberately
fail-loud — a schema error, a duplicate id, a dangling supervisor, an unknown
escalation target, or a missing core role raises :class:`RosterError`, so a
malformed roster refuses to boot rather than running agents with broken
authority. Adding a *new* valid role is a pure config change (a new YAML), which
the loader picks up with zero code edits (TITAN-AGT-006).

Path resolution mirrors the gateway's ``models.yaml`` convention: an absolute
path (the container sets one) is used as-is; a relative path is resolved by
walking up from the process CWD until the directory is found, so the same
default works whether the process runs from the repo root, from ``apps/api``
under pytest, or from ``/app`` in Docker. The parsed roster is cached
process-wide (it is static config), keyed by the resolved directory.
"""

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import structlog
import yaml
from pydantic import ValidationError

from titan.agents.definition import (
    CORE_ROSTER,
    SPECIAL_ESCALATION_TARGETS,
    AgentDefinition,
)
from titan.agents.registry import AgentRegistry

logger = structlog.get_logger("titan.agents.loader")

# The CEO reports to the human client, not another agent (§3.1); every other
# supervisor must resolve to a loaded agent_id.
_ROOT_SUPERVISOR = "human"


class RosterError(ValueError):
    """The agent roster is malformed or incomplete — refuse to boot."""


def resolve_config_dir(configured: str | Path) -> Path:
    """Resolve the agents config dir robustly across run locations.

    Absolute paths are returned as-is (the container sets one). A relative path
    is tried against the CWD and each parent, returning the first existing match
    — but the walk STOPS at the repository root (the first ancestor holding a
    ``.git`` marker) so it can never escape the project and pick up an unrelated
    filesystem-ancestor ``config/agents`` (e.g. a stray ``C:\\config\\agents``).
    If nothing is found within the project the CWD-relative path is returned, so
    the caller fails with a clear in-project "not found" rather than a silent
    wrong roster.
    """
    return _resolve(configured, want_dir=True)


def resolve_config_file(configured: str | Path) -> Path:
    """Like :func:`resolve_config_dir`, but resolves a config FILE (e.g.
    ``config/model-policy.yaml``) rather than a directory."""
    return _resolve(configured, want_dir=False)


def _resolve(configured: str | Path, *, want_dir: bool) -> Path:
    path = Path(configured)
    if path.is_absolute():
        return path
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        candidate = base / path
        if candidate.is_dir() if want_dir else candidate.is_file():
            return candidate
        if (base / ".git").exists():  # repo boundary — do not search above it
            break
    return cwd / path


def _parse_file(path: Path) -> AgentDefinition:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RosterError(f"{path.name}: could not read/parse YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RosterError(f"{path.name}: must be a YAML mapping at the top level")
    try:
        return AgentDefinition.model_validate(data)
    except ValidationError as exc:
        raise RosterError(f"{path.name}: invalid AgentDefinition:\n{exc}") from exc


def _validate_roster(definitions: dict[str, AgentDefinition]) -> None:
    """Cross-referential checks that need the whole roster (§2.1, §3.1)."""
    ids = set(definitions)
    missing = CORE_ROSTER - ids
    if missing:
        raise RosterError(
            f"roster is missing required core agents: {', '.join(sorted(missing))} "
            f"(TITAN-AGT-008 ships exactly the 16-agent roster)"
        )
    for agent_id, definition in definitions.items():
        supervisor = definition.supervisor
        if supervisor != _ROOT_SUPERVISOR and supervisor not in ids:
            raise RosterError(
                f"{agent_id}: supervisor '{supervisor}' is not a known agent "
                f"(use '{_ROOT_SUPERVISOR}' for the top of the org)"
            )
        if supervisor == agent_id:
            raise RosterError(f"{agent_id}: an agent cannot be its own supervisor")
        for escalation in definition.authority.must_escalate:
            for target in escalation.to:
                if target not in ids and target not in SPECIAL_ESCALATION_TARGETS:
                    raise RosterError(
                        f"{agent_id}: must_escalate target '{target}' "
                        f"(decision '{escalation.decision}') is neither a known "
                        f"agent nor a special target {sorted(SPECIAL_ESCALATION_TARGETS)}"
                    )


def build_registry(files: Iterable[Path]) -> AgentRegistry:
    """Parse + validate an explicit set of YAML files into a registry.

    Used directly by tests (temp rosters); production goes through
    :func:`load_roster`, which discovers the files under the config dir.
    """
    definitions: dict[str, AgentDefinition] = {}
    for path in files:
        definition = _parse_file(path)
        if definition.agent_id in definitions:
            raise RosterError(f"duplicate agent_id '{definition.agent_id}' in {path.name}")
        # The filename is the source of truth for the id — a mismatch is almost
        # always a copy-paste error that would silently shadow the wrong role.
        if definition.agent_id != path.stem:
            raise RosterError(
                f"{path.name}: agent_id '{definition.agent_id}' does not match filename"
            )
        definitions[definition.agent_id] = definition
    _validate_roster(definitions)
    return AgentRegistry(definitions)


def load_roster(config_dir: str | Path) -> AgentRegistry:
    """Discover ``<config_dir>/*.yaml`` (non-recursive) and build the registry.

    Non-recursive on purpose: sample/extension rosters live in subdirectories
    (e.g. ``agents/samples/``) so they never join the default roster by accident.
    """
    resolved = resolve_config_dir(config_dir)
    if not resolved.is_dir():
        raise RosterError(f"agents config dir not found: {resolved}")
    files = sorted(resolved.glob("*.yaml"))
    if not files:
        raise RosterError(f"no agent YAML files found in {resolved}")
    registry = build_registry(files)
    logger.info("agent_roster.loaded", count=len(registry), path=str(resolved))
    return registry


@lru_cache(maxsize=8)
def _load_resolved(resolved_dir: str) -> AgentRegistry:
    return load_roster(resolved_dir)


def load_roster_cached(config_dir: str) -> AgentRegistry:
    """Process-wide cached load (the roster is static config). Keyed by the
    RESOLVED absolute directory (not the raw configured string), so the same
    relative default resolved from two different CWDs cannot collide on one
    cache entry. A bad hot-edit does not silently refresh — the process reloads a
    validated roster only on restart, matching the boot-time guarantee."""
    return _load_resolved(str(resolve_config_dir(config_dir)))


__all__ = [
    "RosterError",
    "build_registry",
    "load_roster",
    "load_roster_cached",
    "resolve_config_dir",
    "resolve_config_file",
]
