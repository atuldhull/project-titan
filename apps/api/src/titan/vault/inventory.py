"""Live-inventory feeding for the redaction engine (TITAN-SEC-054 detector 1).

The engine's exact-match detector needs the DIGESTS of every live secret
value. This module is the only place plaintext values and the redactor meet:
it enumerates names (the ``secret_metadata`` table in every vault mode, plus
``store.names()`` when the age vault is unlocked — a crash-orphaned value has
no metadata row but must still be redactable), resolves each through the
provider, hashes them into an :class:`~titan.redaction.engine.InventoryIndex`,
and swaps the index onto the process-global engine. Values never leave this
function's frame.

Refresh is TTL-based (the API and the worker each run a maintenance loop) plus
immediate after every vault write in the API process. A refresh failure keeps
the PREVIOUS index — patterns and entropy keep running regardless, and a stale
inventory redacts everything it did before, so degradation is gradual, never
fail-open to nothing.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.redaction import InventoryIndex, RedactionReporter, default_engine
from titan.vault import service as vault_service
from titan.vault.provider import SecretsProvider, SecretsStore

logger = structlog.get_logger("titan.vault.inventory")

# One loop handles both inventory refresh and audit flushing at the
# composition roots; a secret created on the API node is redactable on the
# worker within this window.
MAINTENANCE_INTERVAL_S = 30.0


async def collect_index(
    provider: SecretsProvider,
    names: set[str],
    *,
    store: SecretsStore | None = None,
) -> InventoryIndex:
    """Resolve *names* (plus the store's own listing) to values and hash them.

    A name that no longer resolves (env mode without the env var, a deleted
    value) is skipped — the index only ever contains digests of values that
    are live right now.
    """
    if store is not None:
        names = names | set(await store.names())
    values: list[str] = []
    for name in sorted(names):
        try:
            values.append(await provider.get(name))
        except KeyError:
            continue
    return InventoryIndex.from_values(values)


async def metadata_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT name FROM secret_metadata"))
        return {row.name for row in rows}


async def refresh_default_engine(
    engine: AsyncEngine,
    provider: SecretsProvider,
    store: SecretsStore | None,
) -> int:
    """Rebuild the digest index and swap it onto the global engine. Returns the
    index size. On failure the previous index stays active."""
    index = await collect_index(provider, await metadata_names(engine), store=store)
    default_engine().set_inventory(index)
    return len(index)


async def run_redaction_maintenance(
    engine: AsyncEngine,
    provider: SecretsProvider,
    store: SecretsStore | None,
    reporter: RedactionReporter,
    *,
    service: str,
    stop: asyncio.Event,
    interval: float = MAINTENANCE_INTERVAL_S,
) -> None:
    """The per-process redaction upkeep loop: refresh the inventory index and
    flush accumulated hit audits. Runs in BOTH the API and the worker (each
    process has its own global engine and its own hits). Self-healing: either
    half failing is logged and retried next tick — the previous index stays
    active, and undrained hits stay queued. The first refresh runs immediately
    so a boot-time secret is redactable before the first tick elapses; a final
    drain on shutdown keeps hits from dying with the process."""
    while not stop.is_set():
        try:
            size = await refresh_default_engine(engine, provider, store)
            logger.debug("redaction.inventory_refreshed", digests=size)
        except Exception:
            logger.exception("redaction.inventory_refresh_error")
        await _flush(engine, reporter, service=service)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    await _flush(engine, reporter, service=service)


async def _flush(engine: AsyncEngine, reporter: RedactionReporter, *, service: str) -> None:
    records = reporter.drain()
    if not records:
        return
    try:
        await vault_service.record_redaction_hits(engine, service=service, records=records)
    except Exception:
        reporter.merge(records)  # keep the hits for the next tick
        logger.exception("redaction.audit_flush_error", records=len(records))


__all__ = [
    "MAINTENANCE_INTERVAL_S",
    "collect_index",
    "metadata_names",
    "refresh_default_engine",
    "run_redaction_maintenance",
]
