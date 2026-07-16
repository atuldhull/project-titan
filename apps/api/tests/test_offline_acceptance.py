"""Full-roster offline acceptance harness (TITAN-AGT-007, Phase 56).

Schema-marked; deterministic via the ScriptedLlm runtime (no Ollama). Proves the
harness runs a real turn for ALL 16 roster roles and assembles a complete
AcceptanceRecord folding in the Phase-55 KPI report — the engineering is verified
independent of local-model quality."""

from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest

from tests.benchmark_helpers import metered_gateway
from tests.conftest import run_async
from tests.test_runtime_turn import ScriptedLlm, _make_project, _report, _resp, _turn_json
from titan.agents.service import CORE_ROSTER
from titan.benchmark.offline_acceptance import (
    ROSTER_TASKS,
    run_offline_acceptance,
    run_roster_turns,
)
from titan.handoffs import gates
from titan.runtime.service import build_runtime
from titan.settings import Settings

pytestmark = pytest.mark.schema


def _roster_script() -> dict[str, list[Any]]:
    # Five identical valid ProgressReports per role — enough for any self-critique
    # retries; roles that reject the shape fail-open/escalate (still a real record).
    resp = _resp(text=_turn_json([_report("ok")]))
    return {f"agent.{role}": [resp for _ in range(5)] for role, _, _ in ROSTER_TASKS}


def _runtime(engine: Any) -> Any:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    runtime = build_runtime(Settings(), engine, redis=fake, llm=ScriptedLlm(_roster_script()))
    runtime.backoff_base_s = 0.0
    return runtime


def test_all_sixteen_roster_roles_run(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        records = await run_roster_turns(_runtime(migrated_engine), pid)
        assert len(records) == 16
        # The roles that ran are EXACTLY the 16-agent roster (catches drift).
        assert {r.role for r in records} == CORE_ROSTER
        # Every record carries the turn's honest outcome.
        assert all(r.outcome for r in records)

    run_async(scenario)


def test_acceptance_record_bundles_kpis(migrated_engine: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        saved = gates.snapshot_gates()
        gateway = metered_gateway(migrated_engine, tmp_path)
        try:
            record = await run_offline_acceptance(
                migrated_engine,
                _runtime(migrated_engine),
                gateway=gateway,
                profile="mock",
                run_id="agt007-test",
            )
        finally:
            await gateway.aclose()
            gates.restore_gates(saved)

        assert record.run_id == "agt007-test"
        assert record.all_roles_ran and len(record.roster_turns) == 16
        assert record.egress_clean  # no guard passed → treated as clean
        # The KPI report is folded in and the pipeline wiring reached release.
        assert record.kpi is not None
        assert record.kpi.suite.crash_recovery_rate == 1.0
        assert record.pipeline_wiring_completes

    run_async(scenario)
