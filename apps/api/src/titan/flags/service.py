"""Feature-flag service — the public surface of ``titan.flags`` (Vol 9 §5.3).

The store is a two-level read-through cache over the ``feature_flags`` table:
an in-process snapshot (10 s TTL) backed by a shared Redis snapshot (10 s TTL),
so a hot-loop check (`agents.pause_all` on every worker dispatch cycle) costs a
dict lookup, not a query. A write commits to Postgres, then DELETEs the shared
Redis snapshot and PUBLISHes a bust on a Redis pub/sub channel; every replica's
:class:`FlagBustSubscriber` drops its in-process copy, so a flag flip converges
across all workers/gateways within ~1 s — well inside the TITAN-OPS-012 15 s
SLA (and bounded by the 10 s TTL even if the bust is missed).

Evaluation (`Flag.is_enabled`): a disabled flag is always false; boolean → the
switch; allowlist → subject ∈ allowlist; percentage → a deterministic
SHA-256 bucket of (key, subject) < percentage (stable across processes, so a
subject's rollout membership never flickers). Unknown type fails closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.flags import repository

logger = structlog.get_logger("titan.flags")

FlagType = Literal["boolean", "percentage", "allowlist"]
FlagClass = Literal["release", "ops", "experiment"]
FlagScope = Literal["global", "org", "project"]

# The three OPS kill switches (TITAN-OPS-012), seeded by migration 0036.
PAUSE_ALL = "agents.pause_all"
INTAKE_READ_ONLY = "intake.read_only"
PROVIDER_OVERRIDE = "llm.provider_override"
# Undeletable by KEY, not just by flag_class: guarding delete on class alone is
# bypassable (PUT flag_class=release, then DELETE), so the kill switches are
# pinned by their keys — they must always exist (TITAN-OPS-012).
KILL_SWITCH_KEYS = frozenset({PAUSE_ALL, INTAKE_READ_ONLY, PROVIDER_OVERRIDE})

DEFAULT_TTL_S = 10
_CACHE_KEY = "titan:flags:snapshot:v1"
BUST_CHANNEL = "titan:flags:bust"
_STALE_AFTER_DAYS = 90


def _bucket(key: str, subject: str) -> int:
    """Deterministic 0..99 bucket for a percentage rollout — SHA-256 so it is
    stable across processes and languages (not Python's salted ``hash``)."""
    digest = hashlib.sha256(f"{key}:{subject}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


@dataclass(frozen=True)
class Flag:
    """One resolved flag definition."""

    key: str
    description: str
    flag_type: str
    flag_class: str
    scope: str
    enabled: bool
    percentage: int | None
    allowlist: tuple[str, ...]
    value: str | None
    owner: str
    expires_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    def is_enabled(self, subject: str | None = None) -> bool:
        if not self.enabled:
            return False
        if self.flag_type == "boolean":
            return True
        if self.flag_type == "allowlist":
            return subject is not None and subject in self.allowlist
        if self.flag_type == "percentage":
            pct = self.percentage or 0
            if pct >= 100:
                return True
            if pct <= 0 or subject is None:
                return False
            return _bucket(self.key, subject) < pct
        return False  # unknown type — fail closed


@dataclass(frozen=True)
class FlagSnapshot:
    """An immutable point-in-time view of every flag."""

    flags: Mapping[str, Flag]

    def get(self, key: str) -> Flag | None:
        return self.flags.get(key)

    def is_enabled(self, key: str, subject: str | None = None) -> bool:
        flag = self.flags.get(key)
        return flag.is_enabled(subject) if flag is not None else False

    def value(self, key: str) -> str | None:
        """The scalar payload of an ENABLED flag (else None) — e.g. the
        ``llm.provider_override`` target alias only applies when the switch is on."""
        flag = self.flags.get(key)
        if flag is None or not flag.enabled:
            return None
        return flag.value

    def resolved(self, subject: str | None = None) -> dict[str, bool]:
        """The resolved on/off view handed to agents via the orchestrator context."""
        return {k: f.is_enabled(subject) for k, f in self.flags.items()}


# ---------------------------------------------------------------------------
# API DTOs (owned here like llm.settings_service owns its Out/Update models).
# ---------------------------------------------------------------------------


class FlagOut(BaseModel):
    key: str
    description: str
    flag_type: FlagType
    flag_class: FlagClass
    scope: FlagScope
    enabled: bool
    percentage: int | None
    allowlist: list[str]
    value: str | None
    owner: str
    expires_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class FlagCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # SEC-070: no silent unknown keys

    key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    description: str = Field(default="", max_length=2000)
    flag_type: FlagType
    flag_class: FlagClass
    scope: FlagScope = "global"
    enabled: bool = False
    percentage: int | None = Field(default=None, ge=0, le=100)
    allowlist: list[str] = Field(default_factory=list, max_length=10_000)
    value: str | None = Field(default=None, max_length=2000)
    owner: str = Field(min_length=1, max_length=200)
    expires_at: datetime.datetime | None = None

    @model_validator(mode="after")
    def _percentage_present(self) -> FlagCreate:
        if self.flag_type == "percentage" and self.percentage is None:
            raise ValueError("a percentage flag requires a percentage")
        return self


class FlagUpdate(BaseModel):
    """Partial update — only the fields the client SENDS are applied
    (``model_dump(exclude_unset=True)``), so ``{"value": null}`` clears the value
    while omitting it leaves it unchanged."""

    model_config = ConfigDict(extra="forbid")  # SEC-070: no silent unknown keys

    description: str | None = Field(default=None, max_length=2000)
    flag_type: FlagType | None = None
    flag_class: FlagClass | None = None
    scope: FlagScope | None = None
    enabled: bool | None = None
    percentage: int | None = Field(default=None, ge=0, le=100)
    allowlist: list[str] | None = Field(default=None, max_length=10_000)
    value: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: datetime.datetime | None = None


class FlagExistsError(Exception):
    """A create hit an existing key (unique violation)."""


class FlagProtectedError(Exception):
    """An ops kill switch may not be deleted — it is a permanent platform control."""


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _row_to_flag(row: Any) -> Flag:
    allowlist = row.allowlist
    if isinstance(allowlist, str):
        allowlist = json.loads(allowlist)
    return Flag(
        key=row.key,
        description=row.description,
        flag_type=row.flag_type,
        flag_class=row.flag_class,
        scope=row.scope,
        enabled=row.enabled,
        percentage=row.percentage,
        allowlist=tuple(allowlist or ()),
        value=row.value,
        owner=row.owner,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def flag_to_out(flag: Flag) -> FlagOut:
    return FlagOut(
        key=flag.key,
        description=flag.description,
        flag_type=flag.flag_type,
        flag_class=flag.flag_class,
        scope=flag.scope,
        enabled=flag.enabled,
        percentage=flag.percentage,
        allowlist=list(flag.allowlist),
        value=flag.value,
        owner=flag.owner,
        expires_at=flag.expires_at,
        created_at=flag.created_at,
        updated_at=flag.updated_at,
    )


def _flag_to_json(f: Flag) -> dict[str, Any]:
    return {
        "key": f.key,
        "description": f.description,
        "flag_type": f.flag_type,
        "flag_class": f.flag_class,
        "scope": f.scope,
        "enabled": f.enabled,
        "percentage": f.percentage,
        "allowlist": list(f.allowlist),
        "value": f.value,
        "owner": f.owner,
        "expires_at": f.expires_at.isoformat() if f.expires_at else None,
        "created_at": f.created_at.isoformat(),
        "updated_at": f.updated_at.isoformat(),
    }


def _json_to_flag(d: dict[str, Any]) -> Flag:
    def _dt(x: str | None) -> datetime.datetime | None:
        return datetime.datetime.fromisoformat(x) if x else None

    created = _dt(d["created_at"])
    updated = _dt(d["updated_at"])
    assert created is not None and updated is not None
    return Flag(
        key=d["key"],
        description=d["description"],
        flag_type=d["flag_type"],
        flag_class=d["flag_class"],
        scope=d["scope"],
        enabled=d["enabled"],
        percentage=d["percentage"],
        allowlist=tuple(d["allowlist"]),
        value=d["value"],
        owner=d["owner"],
        expires_at=_dt(d["expires_at"]),
        created_at=created,
        updated_at=updated,
    )


def _serialize(snap: FlagSnapshot) -> str:
    return json.dumps([_flag_to_json(f) for f in snap.flags.values()])


def _deserialize(raw: str | bytes) -> FlagSnapshot:
    items = json.loads(raw)
    flags = {f.key: f for f in (_json_to_flag(it) for it in items)}
    return FlagSnapshot(flags)


def stale_flags(
    flags: list[Flag], now: datetime.datetime, *, max_age_days: int = _STALE_AFTER_DAYS
) -> list[Flag]:
    """Release/experiment flags older than ``max_age_days`` (by ``created_at``) OR
    past their expiry. OPS kill switches are permanent controls and are exempt —
    CI must not nag about the platform's own kill switches forever (Vol 9 §5.3)."""
    cutoff = now - datetime.timedelta(days=max_age_days)
    out: list[Flag] = []
    for f in flags:
        if f.flag_class == "ops":
            continue
        expired = f.expires_at is not None and f.expires_at < now
        aged = f.created_at < cutoff
        if expired or aged:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class FlagStore:
    """Two-level read-through cache + write-with-bust over ``feature_flags``.

    Reads for the admin API go straight to Postgres (``list_flags``/``get``) so
    an operator always sees fresh state; the hot-path evaluators
    (``is_enabled``/``value``/``snapshot``) use the cached snapshot."""

    def __init__(
        self,
        engine: AsyncEngine,
        redis: Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._engine = engine
        self._redis = redis
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._local: FlagSnapshot | None = None
        self._deadline = 0.0
        self._lock = asyncio.Lock()

    # -- hot path -----------------------------------------------------------

    async def snapshot(self) -> FlagSnapshot:
        local = self._local
        if local is not None and self._clock() < self._deadline:
            return local
        async with self._lock:
            local = self._local
            if local is not None and self._clock() < self._deadline:
                return local
            snap = await self._load()
            self._local = snap
            self._deadline = self._clock() + self._ttl
            return snap

    async def is_enabled(self, key: str, subject: str | None = None) -> bool:
        return (await self.snapshot()).is_enabled(key, subject)

    async def is_enabled_or(self, key: str, default: bool, subject: str | None = None) -> bool:
        """Best-effort ``is_enabled``: returns ``default`` if the store cannot be
        read (a cold snapshot reload hitting a DB blip). Callers on a hot request
        path (intake admission) use this with default=False so a flag-store
        hiccup degrades to accepting + ledgering work (OPS-020 "zero 5xx on
        intake"), never a 500."""
        try:
            return await self.is_enabled(key, subject)
        except Exception:
            logger.warning("flags.read_degraded", flag_key=key)
            return default

    async def value(self, key: str) -> str | None:
        return (await self.snapshot()).value(key)

    def invalidate_local(self) -> None:
        """Drop the in-process snapshot so the next read reloads (pub/sub bust)."""
        self._deadline = 0.0

    async def _load(self) -> FlagSnapshot:
        raw: str | bytes | None = None
        try:
            raw = await self._redis.get(_CACHE_KEY)
        except Exception:
            logger.warning("flags.cache_read_failed")
        if raw is not None:
            with contextlib.suppress(Exception):
                return _deserialize(raw)
        snap = await self._load_from_db()
        with contextlib.suppress(Exception):
            await self._redis.set(_CACHE_KEY, _serialize(snap), ex=self._ttl)
        return snap

    async def _load_from_db(self) -> FlagSnapshot:
        async with self._engine.connect() as conn:
            rows = await repository.list_flags(conn)
        return FlagSnapshot({row.key: _row_to_flag(row) for row in rows})

    # -- admin reads (always fresh) ----------------------------------------

    async def list_flags(self) -> list[Flag]:
        async with self._engine.connect() as conn:
            rows = await repository.list_flags(conn)
        return [_row_to_flag(row) for row in rows]

    async def get(self, key: str) -> Flag | None:
        async with self._engine.connect() as conn:
            row = await repository.get_flag(conn, key)
        return _row_to_flag(row) if row is not None else None

    # -- writes (bust after commit) ----------------------------------------

    async def create(self, spec: FlagCreate) -> Flag:
        try:
            async with self._engine.begin() as conn:
                row = await repository.insert_flag(
                    conn,
                    flag_id=uuid7(),
                    key=spec.key,
                    description=spec.description,
                    flag_type=spec.flag_type,
                    flag_class=spec.flag_class,
                    scope=spec.scope,
                    enabled=spec.enabled,
                    percentage=spec.percentage,
                    allowlist=spec.allowlist,
                    value=spec.value,
                    owner=spec.owner,
                    expires_at=spec.expires_at,
                )
        except IntegrityError as exc:
            raise FlagExistsError(spec.key) from exc
        await self._bust(spec.key)
        return _row_to_flag(row)

    async def update(self, key: str, changes: dict[str, Any]) -> Flag | None:
        async with self._engine.begin() as conn:
            row = await repository.update_flag(conn, key, changes)
        if row is None:
            return None
        await self._bust(key)
        return _row_to_flag(row)

    async def delete(self, key: str) -> bool:
        flag = await self.get(key)
        if flag is None:
            return False
        # Guard by KEY as well as class: checking flag_class alone is bypassable
        # (PUT flag_class=release, then DELETE). A seeded kill switch must always
        # exist (TITAN-OPS-012).
        if flag.flag_class == "ops" or key in KILL_SWITCH_KEYS:
            raise FlagProtectedError(key)
        async with self._engine.begin() as conn:
            deleted = await repository.delete_flag(conn, key)
        if deleted:
            await self._bust(key)
        return deleted

    async def _bust(self, key: str) -> None:
        """Invalidate local + shared caches and signal every replica. Best-effort:
        a Redis hiccup degrades to the 10 s TTL, it never fails the served write."""
        self.invalidate_local()
        try:
            await self._redis.delete(_CACHE_KEY)
            await self._redis.publish(BUST_CHANNEL, key)
        except Exception:
            logger.warning("flags.bust_failed", flag_key=key)


class FlagBustSubscriber:
    """One per replica: drops the local snapshot on a Redis pub/sub bust so a
    flag flip converges faster than the 10 s TTL (mirrors the realtime
    subscriber lifecycle). Best-effort — a subscriber blip only slows
    propagation to the TTL bound, never crashes the process."""

    def __init__(self, redis: Redis, store: FlagStore) -> None:
        self._redis = redis
        self._store = store
        self._task: asyncio.Task[None] | None = None
        self._pubsub: Any = None

    async def start(self) -> None:
        # Subscribe eagerly (so a caller that awaits start() is subscribed before
        # returning) BUT best-effort: a redis hiccup at boot must NEVER fail app
        # startup — the self-healing loop re-subscribes. Without this, a subscribe
        # error here propagated out of the lifespan and took the whole app down.
        try:
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(BUST_CHANNEL)
        except Exception:
            logger.warning("flags.subscriber_start_degraded")
            self._pubsub = None
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                if self._pubsub is None:
                    self._pubsub = self._redis.pubsub()
                    await self._pubsub.subscribe(BUST_CHANNEL)
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None:
                    self._store.invalidate_local()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("flags.subscriber_error")
                self._pubsub = None  # drop it so the next iteration re-subscribes
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None


def build_flag_store(
    engine: AsyncEngine, redis: Redis, *, ttl_seconds: int = DEFAULT_TTL_S
) -> FlagStore:
    return FlagStore(engine, redis, ttl_seconds=ttl_seconds)


__all__ = [
    "BUST_CHANNEL",
    "DEFAULT_TTL_S",
    "INTAKE_READ_ONLY",
    "PAUSE_ALL",
    "PROVIDER_OVERRIDE",
    "Flag",
    "FlagBustSubscriber",
    "FlagCreate",
    "FlagExistsError",
    "FlagOut",
    "FlagProtectedError",
    "FlagSnapshot",
    "FlagStore",
    "FlagUpdate",
    "build_flag_store",
    "flag_to_out",
    "stale_flags",
]
