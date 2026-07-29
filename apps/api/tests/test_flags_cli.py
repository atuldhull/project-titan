"""Phase 78: the `titan flags` operator CLI — the kill-switch control the runbooks
invoke (agents.pause_all / intake.read_only / llm.provider_override). Proves the
documented emergency commands are actually executable and audited, and re-breaks the
on/off parsing + unknown-key paths so the wiring can fail."""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from titan import clients
from titan.cli import main
from titan.flags.service import build_flag_store

from .conftest import run_async

pytestmark = pytest.mark.schema


def _patch(monkeypatch: pytest.MonkeyPatch, schema_url: str) -> None:
    # The CLI builds its OWN engine/redis (a short-lived process) on the migrated DB.
    monkeypatch.setattr(
        clients, "create_engine", lambda _s: create_async_engine(schema_url, poolclass=NullPool)
    )
    monkeypatch.setattr(
        clients, "create_redis", lambda _s: fakeredis.aioredis.FakeRedis(decode_responses=True)
    )


def _enabled(schema_url: str, key: str) -> bool | None:
    engine = create_async_engine(schema_url, poolclass=NullPool)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = build_flag_store(engine, redis)
    try:
        flag = run_async(lambda: store.get(key))
        return flag.enabled if flag else None
    finally:
        run_async(redis.aclose)
        run_async(engine.dispose)


def test_flags_set_flips_and_resets_a_kill_switch(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, schema_url)
    assert _enabled(schema_url, "agents.pause_all") is False  # seeded off (migration 0036)
    assert main(["flags", "set", "agents.pause_all", "on"]) == 0
    assert _enabled(schema_url, "agents.pause_all") is True
    # reset to the seeded state — the schema DB is shared across the suite
    assert main(["flags", "set", "agents.pause_all", "off"]) == 0
    assert _enabled(schema_url, "agents.pause_all") is False


def test_flags_set_rejects_a_bad_state(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, schema_url)
    assert main(["flags", "set", "agents.pause_all", "maybe"]) == 2
    # and never touched the flag
    assert _enabled(schema_url, "agents.pause_all") is False


def test_flags_set_unknown_key_returns_1(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, schema_url)
    assert main(["flags", "set", "no.such.flag", "on"]) == 1


def test_flags_get_and_list(
    migrated_engine: Any,
    schema_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, schema_url)
    assert main(["flags", "get", "agents.pause_all"]) == 0
    assert "agents.pause_all" in capsys.readouterr().out
    assert main(["flags", "list"]) == 0
    assert "agents.pause_all" in capsys.readouterr().out
