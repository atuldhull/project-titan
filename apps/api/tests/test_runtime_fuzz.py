"""Fuzz suite: malformed LLM outputs (risk R4's local-model failure modes) always
follow retry-then-escalate and are NEVER silently accepted (TITAN-AGT-023). Needs
the test DB + fakeredis."""

from typing import Any

import pytest

from tests.conftest import run_async
from tests.test_runtime_turn import ScriptedLlm, _bus, _make_project, _resp, _seed, _turn_json
from titan.runtime.service import build_runtime
from titan.runtime.turn import run_turn
from titan.settings import Settings

pytestmark = pytest.mark.schema

# R4 failure modes a small local model exhibits when asked for structured output.
MALFORMED = [
    ("truncated_json", '{"messages": [{"type": "ProgressReport", "to_agent": "pm'),
    ("not_an_object", "42"),
    ("json_null", "null"),
    ("array_not_object", "[]"),
    ("messages_not_a_list", '{"messages": "ProgressReport now"}'),
    ("empty_messages", '{"messages": []}'),
    ("extra_top_level_field", '{"messages": [], "surprise": 1}'),
    (
        "schema_shaped_prose",
        "Here is my ProgressReport: status in_progress, 50% complete, summary ok.",
    ),
    ("unknown_message_type", '{"messages": [{"type": "Nope", "to_agent": "x", "payload": {}}]}'),
    (
        "wrong_payload_type",
        '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
        '"payload": {"status": "in_progress", "percent_complete": "fifty", "summary": "x"}}]}',
    ),
    (
        "missing_required_payload_field",
        '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
        '"payload": {"status": "in_progress"}}]}',
    ),
    (
        "extra_payload_field",
        '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
        '"payload": {"status": "in_progress", "percent_complete": 50, "summary": "x", '
        '"bogus": 1}}]}',
    ),
    (
        "malformed_task_id",
        '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", "task_id": "nope", '
        '"payload": {"status": "in_progress", "percent_complete": 50, "summary": "x"}}]}',
    ),
]


@pytest.mark.parametrize("label,text", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_output_retries_then_escalates(
    migrated_engine: Any, label: str, text: str
) -> None:
    """Every malformed output is retried (self-critique) and then escalated — never
    silently accepted as a completed turn."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        # Persistently malformed: attempt + both self-critique retries all fail.
        script = {"agent.eng_backend": [_resp(text=text) for _ in range(3)]}
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))

        assert result.outcome != "completed", f"{label} was silently accepted"
        assert result.outcome == "escalated"
        assert result.failure_reason == "invalid_output"
        assert result.output_retries == runtime.max_self_critique  # retried, then escalated
        # retry-THEN-escalate: exactly the one blocked ProgressReport was emitted.
        assert len(result.emitted_message_ids) == 1

    run_async(scenario)


def test_malformed_then_valid_recovers_via_self_critique(migrated_engine: Any) -> None:
    """A malformed first attempt followed by a valid correction completes — the
    self-critique rung recovers without escalating (R4 mitigation)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        valid = _turn_json(
            [
                {
                    "type": "ProgressReport",
                    "to_agent": "pm_project",
                    "payload": {
                        "status": "in_progress",
                        "percent_complete": 50,
                        "summary": "fixed",
                    },
                }
            ]
        )
        script = {"agent.eng_backend": [_resp(text="truncated {"), _resp(text=valid)]}
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.completed and result.output_retries == 1

    run_async(scenario)
