"""Scoped secret injection for task sandboxes (TITAN-SEC-053).

A task's sandbox gets ONLY the secrets its granted tools declare a need for —
resolved by name from the vault and delivered as tmpfs files. The resolution
itself (task → agent → roster grants) is a DOMAIN concern and lives at the
composition root; this module holds only the pure, leaf-safe part: turn a set of
granted tool names into a name→value map via the registry's ``required_secrets``
and the vault provider.

Today no builtin tool declares any ``required_secrets`` (plugin tools run
out-of-process and get secrets over the capability broker, not here), so this
returns an empty map for every current task — which is exactly why a docs task's
sandbox contains zero integration secrets. The mechanism is ready for a future
sandbox-executing tool that genuinely needs an injected credential.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from titan.tools.registry import ToolRegistry
from titan.vault.provider import SecretsProvider

logger = structlog.get_logger("titan.tools.sandbox.injection")


async def resolve_sandbox_secrets(
    tool_names: Iterable[str],
    registry: ToolRegistry,
    provider: SecretsProvider,
) -> dict[str, str]:
    """The scoped secret map for a task: the union of the ``required_secrets``
    declared by ``tool_names`` (the task's granted tools), fetched from the
    vault. A declared-but-unprovisioned secret is skipped with a warning rather
    than blocking the sandbox — a tool degrades, it does not fail to start."""
    needed: set[str] = set()
    for name in tool_names:
        spec = registry.get(name)
        if spec is not None:
            needed.update(spec.required_secrets)
    resolved: dict[str, str] = {}
    for secret_name in sorted(needed):
        try:
            resolved[secret_name] = await provider.get(secret_name)
        except KeyError:
            logger.warning("sandbox.secret_missing", name=secret_name)
    return resolved


__all__ = ["resolve_sandbox_secrets"]
