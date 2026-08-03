"""Runtime-level proof that an unapproved prompt is never served (Phase 83).

A full ``run_turn`` over a runtime whose registry has an unapproved prompt: the
turn fails CLOSED with a terminal ``config_error`` (acked, NOT a poison-retry
loop — the MEDIUM-4 fix) and the LLM gateway is NEVER invoked. This is the
orchestrator-level half of DoD(a) / TITAN-EVO-001, complementing the
composer-level refusal tests.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import run_async
from tests.test_runtime_turn import _bus, _make_project, _seed
from titan.llm.models import ChatRequest, ChatResponse
from titan.runtime.service import build_runtime
from titan.runtime.turn import run_turn
from titan.settings import Settings

pytestmark = pytest.mark.schema

REPO_ROOT = Path(__file__).resolve().parents[3]


class _NeverCalledLlm:
    """A gateway that fails the test if the turn ever reaches an LLM call."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, req: ChatRequest) -> ChatResponse:  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("LLM must not be called when the prompt is unapproved")


def _registry_with_unapproved(tmp: Path, agent: str) -> Path:
    """Copy the real registry to tmp and blank one agent's approval in the manifest."""
    dst = tmp / "evolution"
    shutil.copytree(REPO_ROOT / "evolution", dst)
    manifest = dst / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["prompts"][agent]["approved_by"] = None
    data["prompts"][agent]["approved_at"] = None
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    return dst / "prompts"


def test_unapproved_prompt_turn_fails_closed_without_llm_call(
    migrated_engine: Any, tmp_path: Path
) -> None:
    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        prompts_dir = _registry_with_unapproved(tmp_path, "eng_backend")
        llm = _NeverCalledLlm()
        runtime = build_runtime(
            Settings(agent_prompts_dir=str(prompts_dir)),
            migrated_engine,
            redis=bus._redis,
            llm=llm,
        )
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))

        # Fail-closed, terminal (not the transient "error" that would poison-loop),
        # and no LLM call was ever made.
        assert result.outcome == "config_error"
        assert result.should_ack  # acked → the reaper won't retry it forever
        assert llm.calls == 0

    run_async(scenario)


def test_approved_agent_still_runs_from_the_same_tmp_registry(
    migrated_engine: Any, tmp_path: Path
) -> None:
    # Sanity: blanking eng_backend must not break a different, still-approved agent
    # resolved from the same copied registry (the gate is per-artifact).
    async def scenario() -> None:
        prompts_dir = _registry_with_unapproved(tmp_path, "eng_backend")
        from titan.evolution.resolver import EvolutionResolver

        resolver = EvolutionResolver.load(prompts_dir)
        assert resolver.resolve_prompt("reviewer").sha256  # unaffected

    run_async(scenario)


def test_check_evolution_flags_the_tmp_registry(tmp_path: Path) -> None:
    # The CI gate would also catch this before deploy.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_evolution", REPO_ROOT / "apps" / "api" / "scripts" / "check_evolution.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _registry_with_unapproved(tmp_path, "ceo")
    errs = mod.collect_errors(tmp_path, agents_dir=REPO_ROOT / "config" / "agents")
    assert any("UNAPPROVED" in e for e in errs)
