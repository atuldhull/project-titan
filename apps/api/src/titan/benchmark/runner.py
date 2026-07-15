"""The benchmark runner (Vol 1 §7, TITAN-FND-007/052, Phase 55 DoD item 3).

Executes the one executable brief end-to-end, folds in the chaos suite's crash-
recovery result, and assembles a complete Section-7 KPI report — one record per
brief, the executable one measured and the other nine carried as fixtures.

Crash recovery is SIMULATED (in-process) against an **isolated** queue (a fresh
fakeredis), never shared infrastructure: the ``kill_redis`` scenario flushes the
broker, so it must own it. When that isolation is unavailable the metric is
reported as not-measured rather than run destructively or fabricated.

WARNING: the runner WRITES benchmark + chaos rows (projects, users, messages,
outbox/inbox, llm_usage) to the caller-supplied ``engine`` and does not clean up.
The QUEUE is isolated (in-process fakeredis) but PostgreSQL is NOT — pass a
disposable engine (the CLI gates this behind ``--confirm``; the nightly job uses
the disposable test database).
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.benchmark.execute import execute_tiny_brief
from titan.benchmark.kpi import (
    BriefKpis,
    KpiReport,
    build_mvp_checks,
    build_suite_kpis,
)
from titan.benchmark.suite import BenchmarkSuite, load_suite
from titan.chaos.model import ChaosRunReport
from titan.db import uuid7

_SYSTEM_USER_EMAIL = "benchmark@titan.local"


class BenchmarkRunner:
    """Runs the benchmark suite and produces a :class:`KpiReport`."""

    def __init__(self, engine: AsyncEngine, *, gateway: Any | None = None) -> None:
        self._engine = engine
        self._gateway = gateway

    async def run(
        self,
        *,
        brief_id: str | None = None,
        profile: str = "mock",
        run_id: str | None = None,
        generated_at: str | None = None,
        chaos: ChaosRunReport | None = None,
        suite: BenchmarkSuite | None = None,
    ) -> KpiReport:
        """Execute the (default: the single executable) brief and build the report.
        ``chaos`` may be supplied by a caller that already ran the suite; otherwise
        an isolated run is attempted."""
        loaded = suite or load_suite()
        target = loaded.by_id(brief_id) if brief_id else loaded.executable_brief
        if not target.executable:
            raise ValueError(f"brief {target.id!r} is a fixture, not executable this phase")

        created_by = await self._ensure_system_user()
        result = await execute_tiny_brief(
            self._engine, target, created_by=created_by, gateway=self._gateway
        )

        if chaos is None:
            chaos = await self._maybe_run_chaos()
        crash_rate = chaos.crash_recovery_rate if chaos is not None else None

        per_brief: list[BriefKpis] = []
        for brief in loaded.briefs:
            if brief.id == target.id:
                per_brief.append(
                    BriefKpis(
                        brief_id=brief.id,
                        executed=True,
                        completed=result.completed,
                        first_draft_rejected=result.first_draft_rejected,
                        time_to_first_deploy_seconds=result.time_to_first_deploy_seconds,
                        local_only=brief.local_only,
                        cost_usd=result.cost_usd,
                        cost_measured=result.cost_measured,
                        generated_test_coverage=brief.expected_coverage,
                        coverage_source="declared",
                        human_intervention_count=result.human_intervention_count,
                    )
                )
            else:
                per_brief.append(
                    BriefKpis(
                        brief_id=brief.id,
                        executed=False,
                        local_only=brief.local_only,
                        generated_test_coverage=brief.expected_coverage,
                        coverage_source="declared",
                    )
                )

        suite_kpis = build_suite_kpis(
            per_brief, briefs_total=len(loaded.briefs), crash_recovery_rate=crash_rate
        )
        return KpiReport(
            run_id=run_id or str(uuid7()),
            profile=profile,
            suite_version=loaded.version,
            per_brief=per_brief,
            suite=suite_kpis,
            mvp=build_mvp_checks(suite_kpis),
            generated_at=generated_at,
        )

    async def _maybe_run_chaos(self) -> ChaosRunReport | None:
        """Run the full chaos sweep against a fresh in-process queue. Returns None
        if isolated queue support is unavailable (crash recovery stays unmeasured
        rather than touching shared infrastructure)."""
        try:
            import fakeredis.aioredis
        except ImportError:  # pragma: no cover — fakeredis ships in the test/CI env
            return None
        from titan.chaos.harness import ChaosHarness
        from titan.events.redis_bus import RedisStreamBus

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            bus = RedisStreamBus(redis)
            return await ChaosHarness(self._engine, bus, redis).run_all()
        finally:
            await redis.aclose()

    async def _ensure_system_user(self) -> uuid.UUID:
        """The benchmark's project-owner principal (idempotent)."""
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e"), {"e": _SYSTEM_USER_EMAIL}
                )
            ).first()
            if existing is not None:
                return uuid.UUID(str(existing.id))
            uid = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :e, 'Benchmark', 'local', :s)"
                ),
                {"id": uid, "e": _SYSTEM_USER_EMAIL, "s": str(uid)},
            )
            return uid


__all__ = ["BenchmarkRunner"]
