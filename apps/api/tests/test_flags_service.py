"""Phase 72 (Vol 9 §5.3): the feature-flag store — evaluation, the two-level
cache + pub/sub bust, the write path, and the staleness rule.

The evaluation + staleness + serialization tests are pure; the store tests run
against the real migrated schema (which carries the three seeded kill switches)
+ fakeredis. Every created flag uses a unique key — the schema DB is shared
across the session."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from titan.flags import service as flags
from titan.flags.service import (
    Flag,
    FlagBustSubscriber,
    FlagCreate,
    FlagExistsError,
    FlagProtectedError,
    FlagStore,
    _deserialize,
    _serialize,
    stale_flags,
)

from .conftest import run_async

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _flag(**kw: Any) -> Flag:
    base: dict[str, Any] = dict(
        key="f",
        description="",
        flag_type="boolean",
        flag_class="release",
        scope="global",
        enabled=True,
        percentage=None,
        allowlist=(),
        value=None,
        owner="o",
        expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(kw)
    return Flag(**base)


# --- pure evaluation -------------------------------------------------------


def test_boolean_flag_is_the_switch() -> None:
    assert _flag(flag_type="boolean", enabled=True).is_enabled() is True
    assert _flag(flag_type="boolean", enabled=False).is_enabled() is False


def test_disabled_flag_is_always_false_regardless_of_type() -> None:
    # A disabled percentage/allowlist flag never evaluates true even for a match.
    assert _flag(flag_type="percentage", percentage=100, enabled=False).is_enabled("x") is False
    assert _flag(flag_type="allowlist", allowlist=("x",), enabled=False).is_enabled("x") is False


def test_allowlist_membership() -> None:
    f = _flag(flag_type="allowlist", allowlist=("alice", "bob"))
    assert f.is_enabled("alice") is True
    assert f.is_enabled("carol") is False
    assert f.is_enabled(None) is False  # no subject → not in the list


def test_percentage_bounds_and_determinism() -> None:
    assert _flag(flag_type="percentage", percentage=100).is_enabled("anyone") is True
    assert _flag(flag_type="percentage", percentage=100).is_enabled(None) is True
    assert _flag(flag_type="percentage", percentage=0).is_enabled("anyone") is False
    # A percentage flag needs a stable subject to bucket.
    assert _flag(flag_type="percentage", percentage=50).is_enabled(None) is False
    f = _flag(key="rollout", flag_type="percentage", percentage=50)
    # Deterministic: the same subject always lands the same side.
    assert f.is_enabled("user-42") == f.is_enabled("user-42")
    # And it actually buckets — a 50% flag enables a middling fraction, not all
    # or nothing (a hash that ignored the subject would be 0% or 100%).
    enabled = sum(f.is_enabled(f"user-{i}") for i in range(400))
    assert 120 < enabled < 280


def test_unknown_type_fails_closed() -> None:
    assert _flag(flag_type="quantum", enabled=True).is_enabled("x") is False


def test_snapshot_value_only_when_enabled() -> None:
    snap = flags.FlagSnapshot(
        {
            "on": _flag(key="on", enabled=True, value="local-default"),
            "off": _flag(key="off", enabled=False, value="local-default"),
        }
    )
    assert snap.value("on") == "local-default"
    assert snap.value("off") is None  # value only applies when the switch is on
    assert snap.value("missing") is None


def test_serialization_roundtrip() -> None:
    original = flags.FlagSnapshot(
        {
            "a": _flag(key="a", flag_type="allowlist", allowlist=("x", "y"), expires_at=_NOW),
            "b": _flag(key="b", flag_type="percentage", percentage=25, value="v"),
        }
    )
    restored = _deserialize(_serialize(original))
    assert restored.flags["a"].allowlist == ("x", "y")
    assert restored.flags["a"].expires_at == _NOW
    assert restored.flags["b"].percentage == 25 and restored.flags["b"].value == "v"


# --- staleness rule --------------------------------------------------------


def test_stale_flags_flags_old_and_expired_but_exempts_ops() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    fresh = _flag(key="fresh", created_at=now - timedelta(days=10))
    old = _flag(key="old", created_at=now - timedelta(days=120))
    expired = _flag(key="expired", created_at=now, expires_at=now - timedelta(days=1))
    ops_old = _flag(key="agents.pause_all", flag_class="ops", created_at=now - timedelta(days=1000))
    result = {f.key for f in stale_flags([fresh, old, expired, ops_old], now)}
    assert result == {"old", "expired"}  # ops kill switch is permanent → exempt


# --- the store: reads, cache, write, bust ----------------------------------

pytestmark = pytest.mark.schema


def _store(api: dict[str, Any]) -> tuple[FlagStore, Any]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return FlagStore(api["engine"], redis, ttl_seconds=10), redis


def test_store_reads_the_seeded_kill_switches(api: dict[str, Any]) -> None:
    store, _ = _store(api)
    keys = {f.key for f in run_async(store.list_flags)}
    assert {"agents.pause_all", "intake.read_only", "llm.provider_override"} <= keys
    # All seeded OFF, boolean, ops class.
    pause = run_async(lambda: store.get("agents.pause_all"))
    assert pause is not None and pause.flag_type == "boolean"
    assert pause.flag_class == "ops" and pause.enabled is False


def test_snapshot_is_cached_and_a_full_bust_reloads(api: dict[str, Any]) -> None:
    store, redis = _store(api)
    key = f"cache-probe-{uuid.uuid4().hex[:8]}"
    run_async(
        lambda: store.create(
            FlagCreate(key=key, flag_type="boolean", flag_class="release", owner="o", enabled=True)
        )
    )
    # Warms BOTH cache levels: the in-process snapshot and the shared Redis one.
    assert run_async(lambda: store.is_enabled(key)) is True

    # Mutate the DB out from under the store (NOT via the store, so no bust).
    async def flip_in_db() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text("UPDATE feature_flags SET enabled = false WHERE key = :k"), {"k": key}
            )

    run_async(flip_in_db)
    # Both cache levels are still fresh, so the change is NOT yet visible.
    assert run_async(lambda: store.is_enabled(key)) is True
    # A full bust clears BOTH levels — exactly what _bust() does on a write, so
    # the next read falls through to Postgres and sees the flip. (Clearing only
    # the local snapshot would still read the stale shared snapshot — which is
    # why the writer deletes the shared key AND every replica invalidates local.)
    run_async(lambda: redis.delete("titan:flags:snapshot:v1"))
    store.invalidate_local()
    assert run_async(lambda: store.is_enabled(key)) is False


def test_write_busts_local_and_shared_cache(api: dict[str, Any]) -> None:
    store, redis = _store(api)
    key = f"bust-{uuid.uuid4().hex[:8]}"
    run_async(
        lambda: store.create(
            FlagCreate(key=key, flag_type="boolean", flag_class="release", owner="o", enabled=False)
        )
    )
    # Populate the shared Redis snapshot.
    assert run_async(lambda: store.is_enabled(key)) is False
    assert run_async(lambda: redis.get("titan:flags:snapshot:v1")) is not None
    # A write must DELETE the shared snapshot AND clear the local one, so the
    # very next read reflects the flip (no 10 s TTL wait).
    run_async(lambda: store.update(key, {"enabled": True}))
    assert run_async(lambda: redis.get("titan:flags:snapshot:v1")) is None
    assert run_async(lambda: store.is_enabled(key)) is True


def test_create_conflict_raises(api: dict[str, Any]) -> None:
    store, _ = _store(api)
    key = f"dup-{uuid.uuid4().hex[:8]}"
    spec = FlagCreate(key=key, flag_type="boolean", flag_class="release", owner="o")
    run_async(lambda: store.create(spec))
    with pytest.raises(FlagExistsError):
        run_async(lambda: store.create(spec))


def test_partial_update_leaves_other_fields(api: dict[str, Any]) -> None:
    store, _ = _store(api)
    key = f"partial-{uuid.uuid4().hex[:8]}"
    run_async(
        lambda: store.create(
            FlagCreate(
                key=key,
                flag_type="boolean",
                flag_class="release",
                owner="alice",
                description="keep me",
                enabled=False,
            )
        )
    )
    updated = run_async(lambda: store.update(key, {"enabled": True}))
    assert updated is not None
    assert updated.enabled is True
    assert updated.owner == "alice" and updated.description == "keep me"


def test_ops_kill_switch_cannot_be_deleted(api: dict[str, Any]) -> None:
    store, _ = _store(api)
    with pytest.raises(FlagProtectedError):
        run_async(lambda: store.delete("agents.pause_all"))
    # A non-ops flag deletes fine.
    key = f"del-{uuid.uuid4().hex[:8]}"
    run_async(
        lambda: store.create(
            FlagCreate(key=key, flag_type="boolean", flag_class="experiment", owner="o")
        )
    )
    assert run_async(lambda: store.delete(key)) is True
    assert run_async(lambda: store.delete(key)) is False  # already gone


def test_kill_switch_undeletable_even_after_class_change(api: dict[str, Any]) -> None:
    """The delete guard is keyed on the kill-switch KEY, not just flag_class —
    otherwise an admin could PUT flag_class=release then DELETE it. Re-break:
    guard only on flag_class and this delete succeeds (no FlagProtectedError)."""
    store, _ = _store(api)
    key = "llm.provider_override"
    try:
        run_async(lambda: store.update(key, {"flag_class": "release"}))  # the bypass attempt
        with pytest.raises(FlagProtectedError):
            run_async(lambda: store.delete(key))
    finally:
        # Restore to seeded state (re-create if a broken guard actually deleted it).
        if run_async(lambda: store.get(key)) is None:
            run_async(
                lambda: store.create(
                    FlagCreate(key=key, flag_type="boolean", flag_class="ops", owner="platform")
                )
            )
        else:
            run_async(lambda: store.update(key, {"flag_class": "ops"}))


def test_bust_subscriber_invalidates_local_snapshot(api: dict[str, Any]) -> None:
    """A cross-replica write publishes a bust; the subscriber drops THIS replica's
    local snapshot so the next read reloads. Re-break: without the subscriber the
    stale local snapshot survives until the TTL, so this assertion fails."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reader = FlagStore(api["engine"], redis, ttl_seconds=1000)  # long TTL: only a bust clears it
    writer = FlagStore(api["engine"], redis, ttl_seconds=1000)
    key = f"sub-{uuid.uuid4().hex[:8]}"
    run_async(
        lambda: writer.create(
            FlagCreate(key=key, flag_type="boolean", flag_class="release", owner="o", enabled=False)
        )
    )

    async def scenario() -> bool:
        sub = FlagBustSubscriber(redis, reader)
        await sub.start()
        try:
            assert await reader.is_enabled(key) is False  # warm the reader's local snapshot
            # The writer (a different store instance) flips + publishes a bust.
            await writer.update(key, {"enabled": True})
            # Give the subscriber loop a moment to receive the bust.
            for _ in range(50):
                if await reader.is_enabled(key):
                    return True
                await _yield()
            return await reader.is_enabled(key)
        finally:
            await sub.stop()

    assert run_async(scenario) is True


async def _yield() -> None:
    import asyncio

    await asyncio.sleep(0.02)
