"""Unit tests for the agent runtime's pure pieces — no DB, no Redis:
the model-policy→gateway binding, structured-output validation (the self-critique
driver), the Memory Packet hook, and tool-grant filtering (Vol 2 §8,
TITAN-AGT-004/021/023, TITAN-BRN-009)."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import run_async
from titan.agents.service import ModelPolicyConfig, load_model_policy, load_roster
from titan.runtime.binding import GatewayBinding, ModelBindingError
from titan.runtime.grants import granted_scopes, granted_tool_defs, is_granted
from titan.runtime.memory import (
    MemoryPacket,
    MemoryPacketSections,
    MemoryRequest,
    NullMemoryProvider,
)
from titan.runtime.output import AgentTurnOutput, TurnOutputError, parse_turn_output
from titan.runtime.recovery import LadderConfig, build_escalation
from titan.settings import Settings
from titan.tools.service import ExecutionOutcome, ToolExecutionContext, ToolRegistry, ToolSpec

# --- binding: model-policy → gateway alias (TITAN-AGT-021) -----------------


def _policy() -> ModelPolicyConfig:
    return load_model_policy(Settings())


def test_binding_maps_provider_and_tier_to_alias() -> None:
    binding = GatewayBinding(_policy(), profile=None)
    # architect: reasoning tier on anthropic → the anthropic reasoning alias.
    sel = binding.selection("architect")
    assert sel.tier == "reasoning" and sel.provider == "anthropic"
    assert binding.alias_for(sel) == "reasoning-large"
    # eng_backend: standard tier on anthropic → the anthropic standard alias.
    assert binding.alias_for(binding.selection("eng_backend")) == "coding-default"
    # pm_project: fast tier on openai (§8) → the openai alias (NOT google) — the
    # binding follows the resolved PROVIDER, so the two configs agree.
    pm = binding.selection("pm_project")
    assert pm.tier == "fast" and pm.provider == "openai"
    assert binding.alias_for(pm) == "openai-default"


def test_binding_provider_is_the_load_bearing_switch() -> None:
    """Editing an agent's PROVIDER in model-policy re-selects the gateway alias —
    the config is never inert (TITAN-AGT-021). Simulated by resolving a selection
    whose provider differs and checking the alias follows it."""
    policy = _policy()
    binding = GatewayBinding(policy, profile=None)
    before = binding.selection("architect")  # anthropic/reasoning → reasoning-large
    assert binding.alias_for(before) == "reasoning-large"
    # Same tier, provider switched to openai → a DIFFERENT alias (not anthropic's).
    switched = before.model_copy(update={"provider": "openai"})
    assert binding.alias_for(switched) == "openai-default"
    # An unknown provider is a loud config error, never a silent mis-route.
    with pytest.raises(ModelBindingError):
        binding.alias_for(before.model_copy(update={"provider": "cohere"}))


def test_binding_local_free_profile_routes_to_ollama_alias() -> None:
    """local-free flips every agent's resolved provider to Ollama, so the binding
    routes to the offline alias — the config-only offline path (TITAN-AGT-007)."""
    binding = GatewayBinding(_policy(), profile="local-free")
    for agent in ("architect", "eng_backend", "pm_project"):
        sel = binding.selection(agent)
        assert sel.provider == "ollama"
        assert binding.alias_for(sel) == "local-default"


def test_binding_build_request_carries_temperature_and_agent_metadata() -> None:
    binding = GatewayBinding(_policy(), profile=None)
    req = binding.build_request(
        "architect",
        system="SYS",
        messages=[],
        tools=[],
        max_output_tokens=1024,
        project_id="p1",
        task_id=None,
    )
    assert req.model_alias == "reasoning-large"
    assert req.temperature == binding.selection("architect").temperature
    assert req.max_tokens == 1024
    assert req.metadata.agent_id == "agent.architect"
    assert req.messages[0].role == "system" and req.messages[0].content == "SYS"
    # No granted tools → tools omitted (never an empty list forced on the model).
    assert req.tools is None


# --- structured output validation (TITAN-AGT-023) --------------------------


def test_parse_turn_output_accepts_valid() -> None:
    out = parse_turn_output(
        '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
        '"payload": {"status": "in_progress", "percent_complete": 50, "summary": "ok"}}]}'
    )
    assert isinstance(out, AgentTurnOutput)
    assert len(out.messages) == 1 and out.messages[0].type == "ProgressReport"


def test_parse_turn_output_rejects_non_json() -> None:
    with pytest.raises(TurnOutputError):
        parse_turn_output("here is my plan, not json")


def test_parse_turn_output_rejects_array_and_empty() -> None:
    with pytest.raises(TurnOutputError):
        parse_turn_output("[]")
    with pytest.raises(TurnOutputError):
        parse_turn_output("")
    with pytest.raises(TurnOutputError):
        parse_turn_output(None)


def test_parse_turn_output_rejects_bad_payload() -> None:
    """A malformed §6.2 payload (missing required field) is rejected — the
    self-critique driver, not a silently accepted message."""
    with pytest.raises(TurnOutputError):
        parse_turn_output(
            '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
            '"payload": {"status": "in_progress"}}]}'  # missing percent_complete + summary
        )


def test_parse_turn_output_rejects_unknown_type_and_extra_field() -> None:
    with pytest.raises(TurnOutputError):
        parse_turn_output('{"messages": [{"type": "Nope", "to_agent": "x", "payload": {}}]}')
    with pytest.raises(TurnOutputError):
        parse_turn_output('{"messages": [], "bogus": 1}')


def test_parse_turn_output_rejects_empty_messages() -> None:
    """A turn must emit ≥1 structured message (§2.2) — an empty output is rejected
    (→ self-critique), which also keeps every completed turn crash-idempotent."""
    with pytest.raises(TurnOutputError):
        parse_turn_output('{"messages": []}')
    with pytest.raises(TurnOutputError):
        parse_turn_output("{}")
    with pytest.raises(TurnOutputError):
        parse_turn_output('{"note": "did some thinking"}')


def test_parse_turn_output_rejects_malformed_task_id() -> None:
    """A non-UUID task_id is rejected at parse time so it drives self-critique,
    not a crash inside the emit transaction (TITAN-AGT-023)."""
    with pytest.raises(TurnOutputError):
        parse_turn_output(
            '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
            '"task_id": "not-a-uuid", '
            '"payload": {"status": "in_progress", "percent_complete": 10, "summary": "x"}}]}'
        )


# --- memory packet hook (Vol 5 §13, TITAN-BRN-009) -------------------------


def test_null_memory_provider_returns_valid_identified_empty_packet() -> None:
    async def scenario() -> None:
        packet = await NullMemoryProvider().assemble(
            MemoryRequest(agent_id="eng_backend", project_id=uuid.uuid4(), call_seq=3)
        )
        assert isinstance(packet.packet_id, uuid.UUID)  # one packet_id per call
        assert packet.call_seq == 3
        assert packet.render() == ""  # empty slot until real retrieval lands

    run_async(scenario)


def test_memory_packet_renders_procedural_before_semantic_before_episodic() -> None:
    packet = MemoryPacket(
        agent_id="eng_backend",
        sections=MemoryPacketSections(
            identity_context="TITAN todo app.",
            procedural=("KB: FastAPI checklist",),
            semantic=("auth-service owns /auth/*",),
            episodic=("QA rejected PR #41",),
            working_summary="halfway done",
        ),
    )
    text = packet.render()
    assert text.index("TITAN todo app.") < text.index("FastAPI checklist")
    assert text.index("FastAPI checklist") < text.index("auth-service")
    assert text.index("auth-service") < text.index("QA rejected")
    assert text.index("QA rejected") < text.index("halfway done")


# --- tool-grant filtering (TITAN-AGT-004) ----------------------------------


async def _echo(arguments: dict, context: ToolExecutionContext) -> ExecutionOutcome:  # type: ignore[type-arg]
    return ExecutionOutcome(result={"echo": arguments})


def _echo_spec() -> ToolSpec:
    return ToolSpec(
        name="code.echo",
        provider="builtin:test",
        description="echo",
        risk="read",
        required_scopes=(),
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        executor=_echo,
        timeout_s=5.0,
    )


def test_granted_tool_defs_only_includes_granted_and_registered() -> None:
    registry = ToolRegistry()
    registry.register(_echo_spec())
    # Grants: one registered tool + one not-yet-built tool. Only the registered
    # one contributes a schema; an ungranted registered tool would be excluded.
    defs = granted_tool_defs(registry, ("code.echo", "repo.read"))
    assert [d.name for d in defs] == ["code.echo"]
    # An ungranted tool never appears even though it is registered.
    assert granted_tool_defs(registry, ("repo.read",)) == []


def test_is_granted_and_scopes() -> None:
    assert is_granted(("a", "b"), "a") is True
    assert is_granted(("a", "b"), "c") is False
    registry = ToolRegistry()
    registry.register(_echo_spec())
    assert granted_scopes(registry, ("code.echo",)) == frozenset()


# --- recovery ladder config (§2.2/§4.x, TITAN-AGT-005) ---------------------


def _ladder(agent_id: str) -> LadderConfig:
    definition = load_roster(Settings()).get(agent_id)
    assert definition is not None
    return LadderConfig.from_definition(definition)


def test_ladder_reads_failure_policy_from_yaml() -> None:
    # CEO: no supervisor agent → escalate to human.
    ceo = _ladder("ceo")
    assert ceo.escalates_to_human and ceo.escalation_target() == "human"
    # Security/Reviewer are fail-closed; Docs/Critic are fail-open.
    assert _ladder("security").fail_closed and _ladder("reviewer").fail_closed
    assert _ladder("docs").fail_open and _ladder("critic").fail_open
    # A default agent escalates to its supervisor.
    backend = _ladder("eng_backend")
    assert not backend.escalates_to_human and backend.escalation_target() == "pm_project"


def test_ladder_skip_retries_matches_operation_label() -> None:
    db = _ladder("eng_database")
    assert db.skip_retries_on  # from YAML: "Destructive migrations"
    assert db.skips_retries("Destructive migration: drop users")  # whole-word, plural-tolerant
    assert not db.skips_retries("Add an index to orders")
    # A negated phrase must NOT match (no substring false-positive on "destructive").
    assert not db.skips_retries("Non-destructive migration: add nullable column")
    assert not db.skips_retries("Nondestructive migration: add column")
    assert not db.skips_retries("Reindex the destructive_log table")  # "migration" absent
    # An agent without skip_retries_on never skips.
    assert not _ladder("eng_backend").skips_retries("anything")


def test_ladder_backoff_is_exponential_and_capped() -> None:
    ladder = LadderConfig(backoff_base_s=0.5)
    assert ladder.backoff_delay(0) == 0.5
    assert ladder.backoff_delay(1) == 1.0
    assert ladder.backoff_delay(2) == 2.0
    assert ladder.backoff_delay(100) == 30.0  # capped at MAX_BACKOFF_S


def test_build_escalation_is_a_blocked_progress_report() -> None:
    msg = build_escalation("pm_project", reason="stuck", options="reassign")
    assert msg.type == "ProgressReport" and msg.to_agent == "pm_project" and msg.requires_ack
    assert msg.payload["status"] == "blocked" and msg.payload["summary"] == "stuck"
