"""The process-global default engine.

Logging is process-global and ``configure_logging`` runs BEFORE the vault
backend exists (main.py builds the app's logging first, the vault later in the
lifespan), so the log processor cannot take an engine by injection. Instead
one shared instance is created at import time with patterns + entropy active
— redaction is on from the first log line, fail closed — and the composition
roots ENRICH it in place (``set_inventory``/``set_reporter``) once the vault
is up. Every default-wired sink (gateway, logs, tool bus, output streams)
reads this same instance, so one inventory refresh upgrades all of them.

Tests that mutate the global engine reset it via the autouse conftest fixture
(``reset_default_engine``).
"""

from titan.redaction.engine import EMPTY_INVENTORY, RedactionEngine

_ENGINE = RedactionEngine()


def default_engine() -> RedactionEngine:
    return _ENGINE


def reset_default_engine() -> None:
    """Restore the pristine state (patterns + entropy only, no reporter)."""
    _ENGINE.set_inventory(EMPTY_INVENTORY)
    _ENGINE.set_reporter(None)


__all__ = ["default_engine", "reset_default_engine"]
