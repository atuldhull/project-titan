"""Every LLM call is traceable to its prompt version (Phase 83, TITAN-EVO-015).

Runs a real agent turn (mock LLM) and asserts the ``agent.turn`` span — the span
in effect around the gateway call — carries ``titan.prompt_sha`` equal to the
approved, manifest-pinned hash of the prompt that served the turn. This is the
trace-assertion half of the Phase-83 Definition of Done.
"""

from typing import Any

import pytest

from tests.conftest import run_async
from tests.test_runtime_recovery import _install_span_exporter
from tests.test_runtime_turn import (
    ScriptedLlm,
    _bus,
    _make_project,
    _report,
    _resp,
    _seed,
    _turn_json,
)
from titan.agents.service import build_evolution_resolver
from titan.runtime.service import build_runtime
from titan.runtime.turn import run_turn
from titan.settings import Settings

pytestmark = pytest.mark.schema


def test_turn_span_carries_the_approved_prompt_sha(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script: dict[str, list[Any]] = {
            "agent.eng_backend": [_resp(text=_turn_json([_report("ok")]))]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        expected = build_evolution_resolver(Settings()).resolve_prompt("eng_backend").sha256
        exporter, restore = _install_span_exporter()
        try:
            result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        finally:
            restore()
        assert result.completed

        turn_spans = [s for s in exporter.get_finished_spans() if s.name == "agent.turn"]
        assert turn_spans, "no agent.turn span recorded"
        sha = turn_spans[0].attributes.get("titan.prompt_sha")
        assert sha == expected
        assert isinstance(sha, str) and len(sha) == 64  # a real content hash

    run_async(scenario)
