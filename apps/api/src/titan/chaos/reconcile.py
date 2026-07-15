"""Post-chaos ledger reconciliation (TITAN-FND-052, DoD item 2).

PostgreSQL is the system of record. After any service is killed and recovered,
this job proves **zero acknowledged-task loss** by reconciling three ledgers for
a consumer group:

* ``durable``  — events committed to the ``outbox`` and published to the stream
  (the system of record says these were sent);
* ``acknowledged`` — the ``event_inbox`` rows the consumer wrote when it acked a
  delivery (the side-effect ledger);
* ``pending``  — the broker's pending-entries list, i.e. in-flight work that will
  be redelivered by the reaper.

Every durable event MUST be acknowledged, still pending, or dead-lettered — never
silently dropped. A durable event that is neither acknowledged nor pending is
acknowledged loss (``dropped``); an acknowledgement with no durable source is a
phantom (``orphaned``). Both must be empty for a clean bill of health.
"""

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.chaos.model import ReconciliationReport
from titan.events.consumers import DOMAIN_GROUP
from titan.events.streams import STREAM_EVENTS_DOMAIN, dlq_stream


class PendingInspector(Protocol):
    """The slice of :class:`~titan.events.redis_bus.RedisStreamBus` the
    reconciliation needs — the in-flight pending set and the set of event_ids
    still present in the stream, both keyed by durable event_id, not broker entry
    id. An event still in the stream is recoverable and therefore not lost."""

    async def pending_event_ids(self, topic: str, group: str) -> list[str]: ...
    async def stream_event_ids(self, topic: str) -> list[str]: ...


async def reconcile_domain_ledger(
    engine: AsyncEngine,
    bus: PendingInspector,
    *,
    stream: str = STREAM_EVENTS_DOMAIN,
    group: str = DOMAIN_GROUP,
    event_ids: set[str] | None = None,
    dead_event_ids: set[str] | None = None,
) -> ReconciliationReport:
    """Reconcile the durable outbox against stream acknowledgements for one group.

    ``event_ids`` scopes the reconciliation to exactly one run's events — the
    outbox and inbox are shared, so the chaos run passes the union of its own
    seeded ids to reconcile ITS ledger, immune to unrelated rows. ``None`` scans
    the whole domain ledger (the operator/global view).

    ``dead_event_ids`` are events the caller knows were deliberately dead-lettered
    (genuine poison): durable and unacked-by-side-effect, but NOT loss — they were
    disposed on purpose. The chaos scenarios inject none, so any unaccounted
    durable event is real acknowledged loss.
    """
    dead = dead_event_ids or set()
    async with engine.connect() as conn:
        if event_ids is None:
            durable = {
                str(r.id)
                for r in await conn.execute(
                    text("SELECT id FROM outbox WHERE topic = :t AND published_at IS NOT NULL"),
                    {"t": stream},
                )
            }
            acknowledged = {
                str(r.event_id)
                for r in await conn.execute(
                    text("SELECT event_id FROM event_inbox WHERE consumer_group = :g"),
                    {"g": group},
                )
            }
        else:
            ids = list(event_ids)
            durable = {
                str(r.id)
                for r in await conn.execute(
                    text(
                        "SELECT id FROM outbox WHERE topic = :t AND published_at IS NOT NULL "
                        "AND id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"t": stream, "ids": ids},
                )
            }
            acknowledged = {
                str(r.event_id)
                for r in await conn.execute(
                    text(
                        "SELECT event_id FROM event_inbox WHERE consumer_group = :g "
                        "AND event_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"g": group, "ids": ids},
                )
            }
    pending = set(await bus.pending_event_ids(stream, group))
    in_stream = set(await bus.stream_event_ids(stream))
    if event_ids is not None:
        pending &= event_ids
        in_stream &= event_ids
    # A durable event is LOST only if it is unacknowledged AND no longer anywhere
    # the broker can redeliver it from — not pending, not still in the stream, and
    # not deliberately dead-lettered. An unread-but-present event is recoverable.
    dropped = durable - acknowledged - pending - in_stream - dead
    orphaned = acknowledged - durable
    return ReconciliationReport(
        group=group,
        durable=len(durable),
        acknowledged=len(acknowledged),
        pending=len(pending),
        dropped=sorted(dropped),
        orphaned=sorted(orphaned),
    )


def domain_dlq_stream() -> str:
    """The dead-letter stream reconciliation would inspect for disposed poison."""
    return dlq_stream(STREAM_EVENTS_DOMAIN)


__all__ = ["PendingInspector", "domain_dlq_stream", "reconcile_domain_ledger"]
