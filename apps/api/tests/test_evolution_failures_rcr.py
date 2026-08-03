"""Failure classification + Root-Cause Records (Phase 85, Vol 10 §5) — DB + unit.

Proves the DoD: a database CHECK plus a test guarantees every RCR carries exactly
one valid F-* code from the taxonomy; and an RCR can be created, linked to an EP,
auto-annotated + remediated when that EP merges, and reopened when the same failure
signature recurs (proving the EP ineffective). Classification is fail-safe: any
parser or provider failure yields F-UNK and never blocks failure handling.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import run_async
from titan.agents.service import build_evolution_resolver
from titan.db import uuid7
from titan.evolution import failures
from titan.evolution import repository as repo
from titan.llm.models import ChatResponse, Usage
from titan.settings import Settings

pytestmark = pytest.mark.schema


class _ClassifyGateway:
    """Returns a fixed classifier JSON and records the metadata it was called with."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.meta: Any = None

    async def chat(self, req: Any) -> ChatResponse:
        self.meta = req.metadata
        text_out = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return ChatResponse(
            text=text_out,
            finish_reason="stop",
            usage=Usage(input_tokens=5, output_tokens=5),
            provider="mock",
            model_id="mock/eval",
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


# ---- pure unit: parse_classification -------------------------------------------------

_VALID = [
    "F-REQ",
    "F-CTX",
    "F-GEN",
    "F-STD",
    "F-SEC",
    "F-ORCH",
    "F-TOOL",
    "F-LLM",
    "F-HUM",
    "F-UNK",
]


def test_parse_classification_valid_and_fallbacks() -> None:
    ok = failures.parse_classification(
        'noise {"primary":"F-SEC","secondaries":["F-STD","F-SEC","bogus"],"confidence":1.4} tail',
        _VALID,
    )
    assert ok["primary"] == "F-SEC"
    assert ok["secondaries"] == ["F-STD"]  # dupes + out-of-taxonomy dropped
    assert ok["confidence"] == 1.0  # clamped to [0,1]

    # An out-of-taxonomy primary is not trusted — fall back to F-UNK.
    assert failures.parse_classification('{"primary":"F-BOGUS"}', _VALID)["primary"] == "F-UNK"
    # Non-JSON / empty → F-UNK.
    assert failures.parse_classification("no json here", _VALID)["primary"] == "F-UNK"
    assert failures.parse_classification(None, _VALID)["primary"] == "F-UNK"
    assert (
        failures.parse_classification('{"primary":"F-LLM","confidence":"x"}', _VALID)["confidence"]
        == 0.0
    )


# ---- classify_failure: fail-safe + real taxonomy -------------------------------------


def test_classify_failure_uses_taxonomy_and_meters_off_task() -> None:
    resolver = build_evolution_resolver(Settings())
    gw = _ClassifyGateway({"primary": "F-SEC", "secondaries": ["F-STD"], "confidence": 0.8})
    out = run_async(lambda: failures.classify_failure(gw, resolver, "an SQLi slipped through"))
    assert out["primary"] == "F-SEC" and out["secondaries"] == ["F-STD"]
    # EVO-020: evolution activity is metered off any project/task.
    assert gw.meta.task_id is None and gw.meta.project_id is None
    assert gw.meta.prompt_sha and gw.meta.agent_id == "agent.memory"


def test_classify_failure_is_fail_safe() -> None:
    class _Boom:
        async def chat(self, req: Any) -> Any:
            raise RuntimeError("provider down")

    resolver = build_evolution_resolver(Settings())
    out = run_async(lambda: failures.classify_failure(_Boom(), resolver, "anything"))
    assert out == {"primary": "F-UNK", "secondaries": [], "confidence": 0.0}


# ---- the DoD DB CHECK: exactly one valid F-* code ------------------------------------


def test_rcr_code_check_constraint_rejects_out_of_taxonomy(migrated_engine: Any) -> None:
    async def scenario() -> None:
        async with migrated_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO root_cause_records (id, failure_code, severity, signature, "
                        "summary) VALUES (:id,'F-BOGUS','high','sig','s')"
                    ),
                    {"id": uuid7()},
                )
        # A valid code inserts cleanly.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO root_cause_records (id, failure_code, severity, signature, "
                    "summary) VALUES (:id,'F-SEC','high','sig-ok','s')"
                ),
                {"id": uuid7()},
            )

    run_async(scenario)


def test_rcr_severity_and_status_checks(migrated_engine: Any) -> None:
    async def scenario() -> None:
        async with migrated_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO root_cause_records (id, failure_code, severity, signature, "
                        "summary) VALUES (:id,'F-SEC','catastrophic','s','s')"
                    ),
                    {"id": uuid7()},
                )
        async with migrated_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO root_cause_records (id, failure_code, severity, signature, "
                        "summary, status) VALUES (:id,'F-SEC','high','s','s','wishful')"
                    ),
                    {"id": uuid7()},
                )

    run_async(scenario)


# ---- the DoD lifecycle: create → link EP → merge → recurrence reopen -----------------


def test_rcr_lifecycle_create_link_merge_and_reopen(migrated_engine: Any) -> None:
    async def scenario() -> None:
        sig = f"F-SEC:agent.security:{uuid.uuid4().hex[:8]}"
        created = await failures.open_or_reopen_rcr(
            migrated_engine,
            failure_code="F-SEC",
            severity="high",
            signature=sig,
            summary="auth bypass",
            task_ids=[uuid7()],
            five_whys=["why1", "why2"],
            remediation={"action": "add MFA"},
        )
        assert created is not None and "reopened" not in created
        rcr_id = uuid.UUID(created["id"])
        assert created["status"] == "open" and created["ep_merged"] is False

        # A second open for the SAME signature while still open is a no-op (dedup).
        dup = await failures.open_or_reopen_rcr(
            migrated_engine,
            failure_code="F-SEC",
            severity="high",
            signature=sig,
            summary="auth bypass again",
        )
        assert dup is None

        # Link the remediating EP, then merge it.
        ep = f"EP-{uuid.uuid4().hex[:8]}"
        linked = await repo.link_rcr_ep(migrated_engine, rcr_id, ep)
        assert linked is not None and linked["ep"] == ep
        count = await failures.annotate_merged_ep(migrated_engine, ep)
        assert count == 1
        rows = await repo.list_rcrs(migrated_engine)
        mine = next(r for r in rows if r["id"] == created["id"])
        assert mine["ep_merged"] is True and mine["status"] == "remediated"

        # Recurrence of the SAME signature reopens the remediated RCR (EP ineffective).
        recur = await failures.open_or_reopen_rcr(
            migrated_engine,
            failure_code="F-SEC",
            severity="high",
            signature=sig,
            summary="auth bypass STILL happening",
            task_ids=[uuid7()],
        )
        assert recur is not None and recur.get("reopened") == created["id"]
        rows = await repo.list_rcrs(migrated_engine, status="open")
        reopened = next(r for r in rows if r["id"] == created["id"])
        assert reopened["status"] == "open" and reopened["ep_merged"] is False
        assert len(reopened["task_ids"]) == 2  # the recurrence task was appended

    run_async(scenario)


def test_accepted_risk_recurrence_is_not_reopened(migrated_engine: Any) -> None:
    """A deliberately accepted-risk RCR must NOT be silently reopened on recurrence
    (that would overturn a human decision); the recurring task is appended instead."""

    async def scenario() -> None:
        sig = f"F-LLM:agent.research:{uuid.uuid4().hex[:8]}"
        created = await failures.open_or_reopen_rcr(
            migrated_engine, failure_code="F-LLM", severity="high", signature=sig, summary="flaky"
        )
        assert created is not None
        rcr_id = uuid.UUID(created["id"])
        # A human accepts the risk.
        await repo.override_rcr(migrated_engine, rcr_id, status="accepted_risk")
        # The same signature recurs.
        recur = await failures.open_or_reopen_rcr(
            migrated_engine,
            failure_code="F-LLM",
            severity="high",
            signature=sig,
            summary="flaky again",
            task_ids=[uuid7()],
        )
        assert recur is not None and recur.get("accepted_risk") == created["id"]
        rows = await repo.list_rcrs(migrated_engine)
        mine = next(r for r in rows if r["id"] == created["id"])
        assert mine["status"] == "accepted_risk"  # NOT reopened
        assert len(mine["task_ids"]) == 1  # the recurrence task was appended

    run_async(scenario)


def test_override_rcr_validates_and_updates(migrated_engine: Any) -> None:
    async def scenario() -> None:
        sig = f"F-GEN:agent.eng_frontend:{uuid.uuid4().hex[:8]}"
        created = await failures.open_or_reopen_rcr(
            migrated_engine, failure_code="F-GEN", severity="low", signature=sig, summary="typo"
        )
        assert created is not None
        rcr_id = uuid.UUID(created["id"])
        updated = await repo.override_rcr(
            migrated_engine, rcr_id, failure_code="F-STD", severity="high"
        )
        assert updated is not None
        assert updated["failure_code"] == "F-STD" and updated["severity"] == "high"
        # No fields provided → no-op (None), never a blind UPDATE.
        assert await repo.override_rcr(migrated_engine, rcr_id) is None

    run_async(scenario)


def test_open_rcr_writes_searchable_companion_memory(migrated_engine: Any) -> None:
    """Every RCR gets a companion brain.memory_records row (subtype='rcr', org scope)
    so it is searchable in the Memory Explorer with zero Explorer change."""

    async def scenario() -> None:
        sig = f"F-TOOL:agent.eng_backend:{uuid.uuid4().hex[:8]}"
        created = await failures.open_or_reopen_rcr(
            migrated_engine,
            failure_code="F-TOOL",
            severity="high",
            signature=sig,
            summary="the linter tool crashed",
        )
        assert created is not None
        async with migrated_engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT kind, scope, metadata FROM brain.memory_records "
                            "WHERE metadata->>'record_subtype' = 'rcr' "
                            "AND metadata->>'signature' = :sig"
                        ),
                        {"sig": sig},
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert row["kind"] == "semantic" and row["scope"] == "org"
        assert row["metadata"]["failure_code"] == "F-TOOL"

    run_async(scenario)
