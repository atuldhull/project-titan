"""CI check: warn on feature flags older than 90 days (Vol 9 §5.3, Phase 72).

Release/experiment flags are meant to be short-lived — ship dark, flip after
deploy, then DELETE. One lingering past 90 days (by created_at) or past its
expiry is debt: dead config that still forks behaviour and hides a stale code
path. OPS kill switches are permanent platform controls and are exempt.

Advisory by default (exit 0 = warn); set TITAN_FLAGS_STRICT=1 to fail CI on any
stale flag. The staleness rule itself lives in titan.flags.service.stale_flags
(pure + unit-tested); this script only fetches the live flags and reports.

Usage: uv run --env-file ../../.env python scripts/check_stale_flags.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from titan import clients
from titan.flags.service import Flag, FlagStore, stale_flags
from titan.observability.logging import configure_logging
from titan.settings import get_settings


async def _list_flags() -> list[Flag]:
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    try:
        return await FlagStore(engine, redis).list_flags()
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging(get_settings().log_level, service="titan-flag-audit")
    now = datetime.now(UTC)
    stale = stale_flags(asyncio.run(_list_flags()), now)
    for flag in stale:
        if flag.expires_at is not None and flag.expires_at < now:
            age = f"EXPIRED {flag.expires_at.date()}"
        else:
            age = f"{(now - flag.created_at).days}d old"
        sys.stdout.write(
            f"WARN stale feature flag '{flag.key}' ({age}; class={flag.flag_class}, "
            f"owner={flag.owner}) — retire it or extend its expiry (Vol 9 §5.3).\n"
        )
    if not stale:
        sys.stdout.write("feature flags: none older than 90 days.\n")
        return
    if os.environ.get("TITAN_FLAGS_STRICT") == "1":
        raise SystemExit(f"{len(stale)} stale feature flag(s) and TITAN_FLAGS_STRICT is set.")


if __name__ == "__main__":
    main()
