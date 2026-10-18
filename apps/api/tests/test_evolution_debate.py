"""Multi-model debate protocol (Phase 86, Vol 10 §7, EVO-021/022/023).

Proves the DoD anchor 3: with the ``evolution.debate`` flag OFF no debate can be convened;
with it ON a fixture debate runs end-to-end and produces an ADR (with a dissent section) +
a stored transcript. Plus judge independence + trigger evaluation.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.db import uuid7
from titan.evolution import debate
from titan.llm.models import ChatResponse, Usage

pytestmark = pytest.mark.schema


class _Flags:
    """Minimal flag-store double: the FlagStore itself is Phase-72 tested; here we only
    exercise convene_debate's master-gate check."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    async def is_enabled(self, key: str, subject: str | None = None) -> bool:
        return self._enabled


class _DebateGateway:
    """Mock gateway: a debater gets a position/rebuttal; the judge gets a JSON verdict."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, req: Any) -> ChatResponse:
        agent = req.metadata.agent_id
        self.calls.append(agent)
        if agent == "agent.debate.judge":
            body = json.dumps(
                {
                    "winner": "A",
                    "decision": "Use an idempotency-key + dedup store.",
                    "rationale": "It gives exactly-once under at-least-once delivery.",
                    "dissent": "B argued the operational cost of a dedup store is high.",
                }
            )
        else:
            body = f"Position from {agent}: I recommend an idempotency key."
        return ChatResponse(
            text=body,
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=20),
            provider="mock",
            model_id="mock/debate",
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


async def _seed_project_agent(engine: Any) -> tuple[uuid.UUID, uuid.UUID]:
    pid, aid, uid = uuid7(), uuid7(), uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'e', 'local', :s)"
            ),
            {"id": uid, "e": f"{uid}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, mode, created_by) "
                "VALUES (:id, :n, 'D', 'b', 'autonomous', :u)"
            ),
            {"id": pid, "n": f"dbt-{pid.hex[-12:]}", "u": uid},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, role, name, model_binding, config) "
                "VALUES (:id, :pid, 'architect', 'conv', CAST('{}' AS JSONB), CAST('{}' AS JSONB))"
            ),
            {"id": aid, "pid": pid},
        )
    return pid, aid


# ---- pure: judge independence + triggers ---------------------------------------------


def test_judge_independence() -> None:
    policy = debate.load_policy()
    debaters, judge = debate._panelists(policy)
    assert debate.judge_independence(debaters, judge) is True
    # A judge cloned from a debater (same model + persona) is NOT independent.
    clone = debaters[0]
    assert debate.judge_independence(debaters, clone) is False


def test_should_debate_triggers() -> None:
    policy = debate.load_policy()
    assert debate.should_debate(policy, decision_class="architecture")[0] is True
    assert debate.should_debate(policy, decision_class="trivial")[0] is False
    assert debate.should_debate(policy, decision_class="trivial", confidence=0.2)[0] is True
    assert debate.should_debate(policy, decision_class="trivial", public_api_change=True)[0] is True


def test_judge_protocol_by_class() -> None:
    policy = debate.load_policy()
    assert debate.judge_protocol_for(policy, "security") == "panel"
    assert debate.judge_protocol_for(policy, "ep") == "human"
    assert debate.judge_protocol_for(policy, "routine") == "single"


# ---- DoD anchor 3: flag off refuses; flag on runs E2E --------------------------------


def test_daily_cap_is_enforced() -> None:
    import fakeredis.aioredis

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        pid = uuid7()
        # cap=2 → first two within cap, third over.
        assert await debate.check_and_increment_cap(redis, pid, "2026-08-03", 2) is True
        assert await debate.check_and_increment_cap(redis, pid, "2026-08-03", 2) is True
        assert await debate.check_and_increment_cap(redis, pid, "2026-08-03", 2) is False
        # cap=0 → always denied.
        assert await debate.check_and_increment_cap(redis, uuid7(), "2026-08-03", 0) is False

    run_async(scenario)


def test_convene_enforces_daily_cap(migrated_engine: Any) -> None:
    import fakeredis.aioredis

    async def scenario() -> None:
        pid, aid = await _seed_project_agent(migrated_engine)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        policy = {**debate.load_policy(), "daily_cap_per_project": 1}
        kw: dict[str, Any] = dict(
            question="Q?",
            context="C",
            decision_class="routine",
            policy=policy,
            project_id=pid,
            convening_agent_id=aid,
            redis=redis,
            day="2026-08-03",
        )
        first = await debate.convene_debate(migrated_engine, _DebateGateway(), _Flags(True), **kw)
        assert first.decision_id is not None
        with pytest.raises(debate.DebateCapExceeded):
            await debate.convene_debate(migrated_engine, _DebateGateway(), _Flags(True), **kw)

    run_async(scenario)


def test_flag_off_refuses_to_convene(migrated_engine: Any) -> None:
    async def scenario() -> None:
        policy = debate.load_policy()
        with pytest.raises(debate.DebateDisabled):
            await debate.convene_debate(
                migrated_engine,
                _DebateGateway(),
                _Flags(enabled=False),
                question="Q?",
                context="C",
                decision_class="architecture",
                policy=policy,
            )

    run_async(scenario)


def test_flag_on_runs_debate_end_to_end(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, aid = await _seed_project_agent(migrated_engine)
        policy = debate.load_policy()
        gateway = _DebateGateway()
        result = await debate.convene_debate(
            migrated_engine,
            gateway,
            _Flags(enabled=True),
            question="How do we make webhook processing exactly-once?",
            context="A receiver may get duplicate deliveries.",
            decision_class="architecture",
            policy=policy,
            project_id=pid,
            convening_agent_id=aid,
        )
        # The judge ruled with a dissent recorded.
        assert result.decision.startswith("Use an idempotency-key")
        assert "dedup store" in result.dissent
        assert result.protocol == "panel"  # architecture → 3-judge panel class
        assert result.decision_id is not None
        assert len(result.transcript) >= 4  # framing + 3 positions + rebuttals + judgment

        async with migrated_engine.connect() as conn:
            # The ADR landed in the decision log with the dissent section.
            dec = (
                (
                    await conn.execute(
                        text("SELECT title, consequences FROM decisions WHERE id = :id"),
                        {"id": uuid.UUID(result.decision_id)},
                    )
                )
                .mappings()
                .first()
            )
            assert dec is not None and dec["title"].startswith("Debate:")
            assert "## Dissent" in dec["consequences"]
            # The transcript is stored as project messages on the debate thread.
            n_msgs = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM messages WHERE thread_id = :tid "
                        "AND kind = 'architecture_discussion'"
                    ),
                    {"tid": uuid.UUID(result.thread_id)},
                )
            ).scalar()
            assert int(n_msgs or 0) >= 4
            # And the canonical ADR + dissent is in org memory.
            org = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM brain.memory_records WHERE scope = 'org' AND "
                        "metadata->>'record_subtype' = 'debate_decision'"
                    )
                )
            ).first()
            assert org is not None

    run_async(scenario)
