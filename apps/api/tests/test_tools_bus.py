"""Tool Bus pipeline DoD tests (Phase 18; Vol 7 §4.2, TITAN-INT-020/022/023/024,
TITAN-SEC-031). Needs the disposable DB (schema marker) — every call, including
pre-execution rejections, must land in the immutable tool_audit ledger.

Covers: per-error-code rejection units with schema-pointer messages, the ok
path (audit pair + hash chain + OTel span), risk gating, parameter constraints,
redaction, chain tamper detection, and the builtin task.get / message.post
tools end to end.
"""

import asyncio
import uuid
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.agents.service import AgentCreate, create_agent
from titan.db import uuid7
from titan.orchestrator.service import TaskCreate, create_task
from titan.tools import repository as audit_repo
from titan.tools.bus import ApprovalDecision, ToolBus, ToolCaller, deny_all_approvals
from titan.tools.envelopes import ToolCallEnvelope, new_call
from titan.tools.errors import ToolError
from titan.tools.registry import (
    ExecutionOutcome,
    ProjectToolPolicy,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)
from titan.tools.service import build_registry, verify_audit_chain

from .conftest import run_async

pytestmark = pytest.mark.schema

_SCOPES = frozenset({"test:run"})


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def span_exporter() -> InMemorySpanExporter:
    """Install an in-memory OTel exporter once for this module; the bus's
    ProxyTracer resolves the provider at span time."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def _clean(migrated_engine: AsyncEngine) -> Any:
    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            # DELETE is blocked by the immutability trigger; TRUNCATE does not
            # fire row triggers and is the sanctioned test reset.
            await conn.execute(text("TRUNCATE tool_audit"))

    run_async(clean)
    yield


def _branch_rule(args: dict[str, Any]) -> tuple[Any, str] | None:
    # TITAN-SEC-031 example rule: git writes only to titan/task-* branches.
    if str(args.get("branch", "")).startswith("titan/task-"):
        return None
    return ("PERMISSION_DENIED", "git writes are allowed only on titan/task-* branches")


def _make_registry(calls: dict[str, int]) -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        calls["echo"] += 1
        return ExecutionOutcome(
            result={"echo": args["text"]}, stderr_tail=str(args.get("stderr", ""))
        )

    async def sleepy(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        await asyncio.sleep(1.0)
        return ExecutionOutcome(result={})

    async def boom(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        raise RuntimeError("kapow")

    async def jail(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        raise ToolError("SANDBOX_VIOLATION", "attempted to read /etc/shadow")

    async def badout(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        return ExecutionOutcome(result={"wrong": 1})

    async def leaky(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        return ExecutionOutcome(result={"note": f"use sk-{'a' * 24} to authenticate"})

    async def bad_artifacts(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        # 'size' instead of 'size_bytes' — an ill-behaved (future plugin/MCP)
        # executor shape that must NOT escape invoke() as an exception.
        return ExecutionOutcome(result={}, artifacts=[{"path": "/workspace/x", "size": 1}])

    async def artifact_leak(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        return ExecutionOutcome(
            result={}, artifacts=[{"kind": "file", "path": f"/workspace/sk-{'b' * 24}.txt"}]
        )

    def exploding_rule(_args: dict[str, Any]) -> "tuple[Any, str] | None":
        raise RuntimeError("constraint blew up")

    def spec(name: str, executor: Any, **kw: Any) -> ToolSpec:
        return ToolSpec(
            name=name,
            provider=kw.pop("provider", "builtin:test"),
            description=f"{name} test tool",
            risk=kw.pop("risk", "read"),
            required_scopes=kw.pop("required_scopes", ("test:run",)),
            input_schema=kw.pop(
                "input_schema",
                {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "stderr": {"type": "string"},
                        "api_key": {"type": "string"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            output_schema=kw.pop("output_schema", {"type": "object"}),
            executor=executor,
            timeout_s=kw.pop("timeout_s", 5.0),
            **kw,
        )

    registry.register(
        spec(
            "test.echo",
            echo,
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
            },
        )
    )
    registry.register(spec("test.sleepy", sleepy, timeout_s=0.05))
    registry.register(spec("test.boom", boom))
    registry.register(spec("test.jail", jail))
    registry.register(
        spec(
            "test.badout",
            badout,
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
            },
        )
    )
    registry.register(spec("test.leaky", leaky))
    registry.register(spec("test.bad_artifacts", bad_artifacts))
    registry.register(spec("test.artifact_leak", artifact_leak))
    registry.register(spec("test.crashy_gate", echo, constraints=(exploding_rule,)))
    registry.register(spec("test.wipe", boom, risk="admin"))
    registry.register(
        spec(
            "test.commit",
            echo,
            risk="write",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}, "branch": {"type": "string"}},
                "required": ["text", "branch"],
                "additionalProperties": False,
            },
            constraints=(_branch_rule,),
        )
    )
    return registry


@pytest.fixture
def harness(migrated_engine: AsyncEngine) -> "tuple[ToolBus, ToolCaller, dict[str, int]]":
    calls = {"echo": 0}
    bus = ToolBus(_make_registry(calls), migrated_engine)
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=uuid.uuid4(),
        granted_scopes=_SCOPES,
        task_id=uuid.uuid4(),
        initiator_chain=("user:founder", "agent.ceo"),
    )
    return bus, caller, calls


def _call(caller: ToolCaller, tool: str, arguments: dict[str, Any]) -> ToolCallEnvelope:
    return new_call(
        tool,
        arguments,
        project_id=caller.project_id,
        agent_id=caller.agent_id,
        task_id=caller.task_id,
    )


def _invoke(bus: ToolBus, call: ToolCallEnvelope, caller: ToolCaller, **kw: Any) -> Any:
    return run_async(lambda: bus.invoke(call, caller, **kw))


def _audit_rows(engine: AsyncEngine, tool_call_id: str) -> list[Any]:
    return list(run_async(lambda: _rows_async(engine, tool_call_id)))


async def _rows_async(engine: AsyncEngine, tool_call_id: str) -> list[Any]:
    async with engine.connect() as conn:
        return await audit_repo.rows_for_call(conn, tool_call_id)


# --- the ok path: pipeline + audit pair + hash chain + OTel span (DoD) --------


def test_ok_path_audit_pair_chain_and_span(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
    span_exporter: InMemorySpanExporter,
) -> None:
    bus, caller, calls = harness
    span_exporter.clear()
    call = _call(caller, "test.echo", {"text": "hello bus"})
    result = _invoke(bus, call, caller)

    # §4.2 result envelope, field for field.
    assert result.status == "ok"
    assert result.tool_call_id == call.tool_call_id
    assert result.result == {"echo": "hello bus"}
    assert result.duration_ms >= 0
    assert result.stderr_tail == "" and result.artifacts == []
    assert calls["echo"] == 1

    # Audit pair: started before dispatch, ok after — chained.
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    assert [r.status for r in rows] == ["started", "ok"]
    started, done = rows
    assert started.prev_hash == audit_repo.GENESIS_HASH
    assert done.prev_hash == started.row_hash
    assert done.duration_ms == result.duration_ms and done.result_hash is not None
    assert done.initiator_chain == ["user:founder", "agent.ceo", "agent.backend.1"]
    assert started.trace_id == call.trace_id

    # Chain verifies end to end.
    assert run_async(lambda: _verify(migrated_engine, caller.project_id)) is None

    # An OTel span was emitted with the tool attributes (TITAN-INT-023).
    spans = [s for s in span_exporter.get_finished_spans() if s.name == "tool.test.echo"]
    assert spans, "no tool.test.echo span exported"
    attrs = spans[-1].attributes or {}
    assert attrs["titan.tool"] == "test.echo"
    assert attrs["titan.tool.status"] == "ok"
    assert attrs["titan.tool_call_id"] == call.tool_call_id


async def _verify(engine: AsyncEngine, project_id: uuid.UUID) -> int | None:
    async with engine.connect() as conn:
        return await verify_audit_chain(conn, project_id)


# --- per-error-code rejection units (DoD: unit tests per error code) ----------


def test_schema_invalid_wrong_type_has_pointer_and_skips_executor(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, calls = harness
    call = _call(caller, "test.echo", {"text": 42})
    result = _invoke(bus, call, caller)
    assert result.status == "error" and result.code == "SCHEMA_INVALID"
    assert "'/text'" in (result.message or "")  # schema-pointer for self-correction
    assert "not of type 'string'" in (result.message or "")
    assert calls["echo"] == 0  # rejected PRE-execution
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    assert [r.status for r in rows] == ["error"]  # rejections are audited too
    assert rows[0].error_code == "SCHEMA_INVALID"


def test_schema_invalid_missing_required_and_extra_property(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    result = _invoke(bus, _call(caller, "test.echo", {"bogus": "x"}), caller)
    assert result.code == "SCHEMA_INVALID"
    message = result.message or ""
    assert "'text' is a required property" in message
    assert "bogus" in message  # additionalProperties violation named


def test_tool_unavailable_unknown_tool(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    call = _call(caller, "test.nonexistent", {})
    result = _invoke(bus, call, caller)
    assert result.code == "TOOL_UNAVAILABLE"
    assert _audit_rows(migrated_engine, call.tool_call_id)[0].error_code == "TOOL_UNAVAILABLE"


def test_tool_unavailable_policy_disabled(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, calls = harness
    policy = ProjectToolPolicy(disabled_tools=["test.*"])
    result = _invoke(bus, _call(caller, "test.echo", {"text": "hi"}), caller, policy=policy)
    assert result.code == "TOOL_UNAVAILABLE" and calls["echo"] == 0


def test_permission_denied_role_allowlist(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, calls = harness
    # A reviewer may not run test.* tools (Vol 8 §4.3: no code write / shell).
    reviewer = ToolCaller(
        agent_id=caller.agent_id,
        role="reviewer",
        project_id=caller.project_id,
        granted_scopes=_SCOPES,
        task_id=caller.task_id,
    )
    call = _call(reviewer, "test.echo", {"text": "hi"})
    result = _invoke(bus, call, reviewer)
    assert result.code == "PERMISSION_DENIED" and "allowlist" in (result.message or "")
    assert calls["echo"] == 0
    assert _audit_rows(migrated_engine, call.tool_call_id)[0].error_code == "PERMISSION_DENIED"


def test_permission_denied_missing_scope(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    unscoped = ToolCaller(
        agent_id=caller.agent_id,
        role="backend",
        project_id=caller.project_id,
        granted_scopes=frozenset(),
        task_id=caller.task_id,
    )
    result = _invoke(bus, _call(unscoped, "test.echo", {"text": "hi"}), unscoped)
    assert result.code == "PERMISSION_DENIED"
    assert "test:run" in (result.message or "")


def test_permission_denied_cross_project_and_spoofed_identity(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, calls = harness
    # TITAN-SEC-031: the call must stay inside the agent's bound project.
    foreign = new_call(
        "test.echo",
        {"text": "hi"},
        project_id=uuid.uuid4(),
        agent_id=caller.agent_id,
        task_id=caller.task_id,
    )
    assert _invoke(bus, foreign, caller).code == "PERMISSION_DENIED"
    # And attribution cannot be spoofed.
    spoofed = new_call(
        "test.echo",
        {"text": "hi"},
        project_id=caller.project_id,
        agent_id="agent.ceo.1",
        task_id=caller.task_id,
    )
    assert _invoke(bus, spoofed, caller).code == "PERMISSION_DENIED"
    # And the call may not reference a different task than the one in flight.
    wrong_task = new_call(
        "test.echo",
        {"text": "hi"},
        project_id=caller.project_id,
        agent_id=caller.agent_id,
        task_id=uuid.uuid4(),
    )
    assert _invoke(bus, wrong_task, caller).code == "PERMISSION_DENIED"
    assert calls["echo"] == 0


def test_admin_risk_blocks_without_approval_and_runs_with_it(
    _clean: Any, migrated_engine: AsyncEngine
) -> None:
    # TITAN-INT-022: admin risk fails closed until Phase 19's approval records.
    calls = {"echo": 0}
    registry = _make_registry(calls)
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=uuid.uuid4(),
        granted_scopes=_SCOPES,
    )
    denied = _invoke(
        ToolBus(registry, migrated_engine, approval_gate=deny_all_approvals),
        new_call(
            "test.wipe", {"text": "x"}, project_id=caller.project_id, agent_id=caller.agent_id
        ),
        caller,
    )
    assert denied.code == "PERMISSION_DENIED" and "approval" in (denied.message or "")

    async def approve_all(_call: ToolCallEnvelope, _spec: ToolSpec) -> ApprovalDecision:
        return ApprovalDecision(True)

    approved = _invoke(
        ToolBus(registry, migrated_engine, approval_gate=approve_all),
        new_call(
            "test.wipe", {"text": "x"}, project_id=caller.project_id, agent_id=caller.agent_id
        ),
        caller,
    )
    # The gate opened; the (deliberately broken) executor then failed — proof
    # the call reached dispatch.
    assert approved.code == "UPSTREAM_ERROR"


def test_param_constraint_enforces_branch_rule(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, calls = harness
    bad = _invoke(bus, _call(caller, "test.commit", {"text": "fix", "branch": "main"}), caller)
    assert bad.code == "PERMISSION_DENIED"
    assert "titan/task-" in (bad.message or "")
    assert calls["echo"] == 0
    good = _invoke(
        bus, _call(caller, "test.commit", {"text": "fix", "branch": "titan/task-42"}), caller
    )
    assert good.status == "ok" and calls["echo"] == 1


def test_timeout_enforced(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    call = _call(caller, "test.sleepy", {"text": "zzz"})
    result = _invoke(bus, call, caller)
    assert result.status == "error" and result.code == "TIMEOUT"
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    assert [r.status for r in rows] == ["started", "error"]
    assert rows[1].error_code == "TIMEOUT"


def test_sandbox_violation_surfaces_and_audits(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    call = _call(caller, "test.jail", {"text": "x"})
    result = _invoke(bus, call, caller)
    assert result.code == "SANDBOX_VIOLATION"
    assert _audit_rows(migrated_engine, call.tool_call_id)[-1].error_code == "SANDBOX_VIOLATION"


def test_upstream_error_from_executor_exception(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    result = _invoke(bus, _call(caller, "test.boom", {"text": "x"}), caller)
    assert result.code == "UPSTREAM_ERROR" and "kapow" in (result.message or "")


def test_malformed_executor_artifacts_become_upstream_error(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    # Envelope-shaping failures must yield an audited error envelope — never an
    # exception escaping invoke() with a dangling 'started' row.
    bus, caller, _ = harness
    call = _call(caller, "test.bad_artifacts", {"text": "x"})
    result = _invoke(bus, call, caller)
    assert result.status == "error" and result.code == "UPSTREAM_ERROR"
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    assert [r.status for r in rows] == ["started", "error"]


def test_artifact_paths_pass_through_the_redactor(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    result = _invoke(bus, _call(caller, "test.artifact_leak", {"text": "x"}), caller)
    assert result.status == "ok"
    assert "sk-" not in result.artifacts[0].path
    assert "[REDACTED]" in result.artifacts[0].path


def test_pre_dispatch_crash_is_audited_not_raised(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    # A constraint (or approval gate) that itself raises must fail closed into
    # an audited UPSTREAM_ERROR — the call never dispatches, never crashes.
    bus, caller, calls = harness
    call = _call(caller, "test.crashy_gate", {"text": "x"})
    result = _invoke(bus, call, caller)
    assert result.status == "error" and result.code == "UPSTREAM_ERROR"
    assert calls["echo"] == 0
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    assert [r.status for r in rows] == ["error"]


def test_task_binding_is_strict_both_directions(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, calls = harness
    # A tasked caller may not shed its task attribution…
    untasked_call = new_call(
        "test.echo", {"text": "hi"}, project_id=caller.project_id, agent_id=caller.agent_id
    )
    assert _invoke(bus, untasked_call, caller).code == "PERMISSION_DENIED"
    # …and an untasked caller may not claim a task.
    untasked_caller = ToolCaller(
        agent_id=caller.agent_id,
        role="backend",
        project_id=caller.project_id,
        granted_scopes=_SCOPES,
    )
    tasked_call = new_call(
        "test.echo",
        {"text": "hi"},
        project_id=caller.project_id,
        agent_id=caller.agent_id,
        task_id=uuid.uuid4(),
    )
    assert _invoke(bus, tasked_call, untasked_caller).code == "PERMISSION_DENIED"
    assert calls["echo"] == 0


def test_upstream_error_on_output_schema_violation(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    result = _invoke(bus, _call(caller, "test.badout", {"text": "x"}), caller)
    assert result.code == "UPSTREAM_ERROR"
    assert "output schema" in (result.message or "")
    assert "'echo' is a required property" in (result.message or "")


# --- redaction (TITAN-INT-023: no secret material in audit or results) --------


def test_arguments_redacted_in_audit_but_hash_covers_raw(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    secret_args = {"text": "hello", "api_key": "sk-verysecretkey12345678"}
    call = _call(caller, "test.echo", secret_args)
    result = _invoke(bus, call, caller)
    assert result.status == "ok"
    rows = _audit_rows(migrated_engine, call.tool_call_id)
    for row in rows:
        assert row.args_redacted["api_key"] == "[REDACTED]"
        assert "verysecret" not in str(row.args_redacted)
        # The hash attests the RAW arguments, not the redacted copy.
        assert row.args_hash == audit_repo.canonical_hash(secret_args)


def test_result_redacted_before_return(
    _clean: Any, harness: "tuple[ToolBus, ToolCaller, dict[str, int]]"
) -> None:
    bus, caller, _ = harness
    result = _invoke(bus, _call(caller, "test.leaky", {"text": "x"}), caller)
    assert result.status == "ok"
    assert "sk-" not in result.result["note"]
    assert "[REDACTED]" in result.result["note"]


# --- immutability + hash chain (TITAN-INT-023/024) -----------------------------


def test_tool_audit_rejects_update_and_delete(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    _invoke(bus, _call(caller, "test.echo", {"text": "immutable"}), caller)

    async def mutate(statement: str) -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text(statement))

    with pytest.raises(DBAPIError, match="append-only"):
        run_async(lambda: mutate("UPDATE tool_audit SET tool = 'forged'"))
    with pytest.raises(DBAPIError, match="append-only"):
        run_async(lambda: mutate("DELETE FROM tool_audit"))


def test_verify_chain_detects_mutated_historical_row(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    bus, caller, _ = harness
    for i in range(3):
        _invoke(bus, _call(caller, "test.echo", {"text": f"call {i}"}), caller)
    assert run_async(lambda: _verify(migrated_engine, caller.project_id)) is None

    # TITAN-INT-024 acceptance: mutate a historical row past the trigger
    # (as a privileged attacker would) — verification must detect it.
    async def tamper() -> int:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE tool_audit DISABLE TRIGGER trg_tool_audit_immutable")
            )
            row = await conn.execute(
                text(
                    "UPDATE tool_audit SET agent_id = 'agent.evil' WHERE seq = ("
                    "SELECT min(seq) FROM tool_audit WHERE project_id = :p) RETURNING seq"
                ),
                {"p": caller.project_id},
            )
            await conn.execute(
                text("ALTER TABLE tool_audit ENABLE TRIGGER trg_tool_audit_immutable")
            )
            return int(row.scalar_one())

    mutated_seq = run_async(tamper)
    assert run_async(lambda: _verify(migrated_engine, caller.project_id)) == mutated_seq


def test_verify_chain_detects_mutated_args_redacted(
    _clean: Any,
    harness: "tuple[ToolBus, ToolCaller, dict[str, int]]",
    migrated_engine: AsyncEngine,
) -> None:
    # args_redacted is the ONLY stored copy of the arguments — rewriting it
    # must break the chain even though args_hash (raw attestation) is untouched.
    bus, caller, _ = harness
    for i in range(2):
        _invoke(bus, _call(caller, "test.echo", {"text": f"call {i}"}), caller)
    assert run_async(lambda: _verify(migrated_engine, caller.project_id)) is None

    async def tamper() -> int:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE tool_audit DISABLE TRIGGER trg_tool_audit_immutable")
            )
            row = await conn.execute(
                text(
                    'UPDATE tool_audit SET args_redacted = \'{"text": "harmless"}\'::jsonb '
                    "WHERE seq = (SELECT min(seq) FROM tool_audit WHERE project_id = :p) "
                    "RETURNING seq"
                ),
                {"p": caller.project_id},
            )
            await conn.execute(
                text("ALTER TABLE tool_audit ENABLE TRIGGER trg_tool_audit_immutable")
            )
            return int(row.scalar_one())

    mutated_seq = run_async(tamper)
    assert run_async(lambda: _verify(migrated_engine, caller.project_id)) == mutated_seq


def test_verify_chain_empty_project_is_intact(_clean: Any, migrated_engine: AsyncEngine) -> None:
    assert run_async(lambda: _verify(migrated_engine, uuid.uuid4())) is None


# --- builtin tools end to end (task.get, message.post) -------------------------


@pytest.fixture
def seeded(migrated_engine: AsyncEngine) -> dict[str, Any]:
    """A real user → project → task → agent graph for the builtin bridges."""

    async def seed() -> dict[str, Any]:
        user_id, project_id = uuid7(), uuid7()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Tool Bus Test', 'local', :subj)"
                ),
                {"id": user_id, "email": f"tools-{user_id}@titan.local", "subj": str(user_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, created_by) "
                    "VALUES (:id, :name, 'Tool Bus Project', 'exercise the bus', :uid)"
                ),
                {"id": project_id, "name": f"tools-{project_id}", "uid": user_id},
            )
            task = await create_task(
                conn, project_id, TaskCreate(title="Ship the tool bus"), actor="test"
            )
            agent = await create_agent(
                conn,
                AgentCreate(
                    role="pm",
                    name="PM One",
                    model_binding={"provider": "ollama"},
                    project_id=project_id,
                ),
            )
        return {"project_id": project_id, "task": task, "agent": agent}

    return dict(run_async(seed))


def test_builtin_task_get_and_scope_jail(
    _clean: Any, seeded: dict[str, Any], migrated_engine: AsyncEngine
) -> None:
    bus = ToolBus(build_registry(), migrated_engine)
    caller = ToolCaller(
        agent_id="agent.pm.1",
        role="pm",
        project_id=seeded["project_id"],
        granted_scopes=frozenset({"*:read", "message:write"}),
        task_id=seeded["task"].id,
        agent_uuid=seeded["agent"].id,
    )
    call = _call(caller, "task.get", {"task_id": str(seeded["task"].id)})
    result = _invoke(bus, call, caller)
    assert result.status == "ok"
    assert result.result["title"] == "Ship the tool bus"
    assert result.result["project_id"] == str(seeded["project_id"])

    # A task outside the caller's project is invisible — identical error to a
    # missing one (TITAN-SEC-032, no existence leak).
    missing = _invoke(bus, _call(caller, "task.get", {"task_id": str(uuid.uuid4())}), caller)
    assert missing.code == "PERMISSION_DENIED"
    assert "no such task" in (missing.message or "")


def test_builtin_task_get_rejects_non_uuid_pre_execution(
    _clean: Any, seeded: dict[str, Any], migrated_engine: AsyncEngine
) -> None:
    bus = ToolBus(build_registry(), migrated_engine)
    caller = ToolCaller(
        agent_id="agent.pm.1",
        role="pm",
        project_id=seeded["project_id"],
        granted_scopes=frozenset({"*:read"}),
        task_id=seeded["task"].id,
    )
    result = _invoke(bus, _call(caller, "task.get", {"task_id": "../../etc/passwd"}), caller)
    assert result.code == "SCHEMA_INVALID"  # format: uuid, checked pre-execution
    assert "'/task_id'" in (result.message or "")


def test_builtin_message_post_writes_through_service(
    _clean: Any, seeded: dict[str, Any], migrated_engine: AsyncEngine
) -> None:
    bus = ToolBus(build_registry(), migrated_engine)
    caller = ToolCaller(
        agent_id="agent.pm.1",
        role="pm",
        project_id=seeded["project_id"],
        granted_scopes=frozenset({"message:write"}),
        task_id=seeded["task"].id,
        agent_uuid=seeded["agent"].id,
    )
    call = _call(
        caller, "message.post", {"kind": "progress_report", "body": {"note": "bus is live"}}
    )
    result = _invoke(bus, call, caller)
    assert result.status == "ok"

    async def sender() -> Any:
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT sender_agent_id, kind FROM messages WHERE id = :id"),
                {"id": uuid.UUID(result.result["message_id"])},
            )
            return row.one()

    persisted = run_async(sender)
    # Attribution is the bus-authenticated agent, never model input.
    assert persisted.sender_agent_id == seeded["agent"].id
    assert persisted.kind == "progress_report"


def test_builtin_message_post_without_agent_identity_denied(
    _clean: Any, seeded: dict[str, Any], migrated_engine: AsyncEngine
) -> None:
    bus = ToolBus(build_registry(), migrated_engine)
    caller = ToolCaller(
        agent_id="agent.pm.1",
        role="pm",
        project_id=seeded["project_id"],
        granted_scopes=frozenset({"message:write"}),
        agent_uuid=None,
    )
    result = _invoke(bus, _call(caller, "message.post", {"kind": "human", "body": {}}), caller)
    assert result.code == "PERMISSION_DENIED"
    assert "agent identity" in (result.message or "")
