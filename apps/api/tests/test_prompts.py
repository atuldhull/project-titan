"""System-prompt composition engine (Vol 2 §8, TITAN-AGT-021, R5).

Every agent's prompt is assembled in the normative 7-part order, and the
authority section is RENDERED FROM the agent's YAML — no template hard-codes it,
so editing the YAML's authority list changes the prompt with no template edit
(the R5 authority-drift guard). Pure unit tests: no DB, no Redis.
"""

from pathlib import Path
from typing import Any

import pytest

from titan.agents.definition import Authority, Escalation
from titan.agents.loader import build_registry, load_roster
from titan.agents.prompts import SECTION_MARKERS, PromptComposer
from titan.evolution.resolver import EvolutionResolver

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "config" / "agents"
# Phase 83: prompts live in the SHA-pinned evolution registry, not config/prompts.
PROMPTS_DIR = REPO_ROOT / "evolution" / "prompts"
SAMPLE_LOCALIZATION = AGENTS_DIR / "samples" / "localization.yaml"


@pytest.fixture(scope="module")
def roster() -> Any:
    return load_roster(AGENTS_DIR)


@pytest.fixture(scope="module")
def composer() -> Any:
    return PromptComposer(EvolutionResolver.load(PROMPTS_DIR))


def _section_indices(prompt: str) -> list[int]:
    return [prompt.find(m) for m in SECTION_MARKERS]


def test_composed_prompt_has_seven_sections_in_order(roster: Any, composer: Any) -> None:
    prompt = composer.compose(
        roster.get("ceo"), task_context="Build a todo app.", memory="(vision v1)"
    )
    idx = _section_indices(prompt)
    assert all(i >= 0 for i in idx), "a section marker is missing"
    assert idx == sorted(idx), "sections are out of the normative order"


@pytest.mark.parametrize(
    "agent_id",
    [
        "ceo",
        "orchestrator",
        "pm_product",
        "architect",
        "pm_project",
        "eng_backend",
        "eng_frontend",
        "eng_database",
        "eng_devops",
        "qa",
        "security",
        "reviewer",
        "docs",
        "research",
        "memory",
        "critic",
    ],
)
def test_every_template_renders_with_all_sections(
    roster: Any, composer: Any, agent_id: str
) -> None:
    d = roster.get(agent_id)
    prompt = composer.compose(d, task_context="ctx", memory="mem")
    assert all(i >= 0 for i in _section_indices(prompt))
    # Identity block names the role, and the authority items appear (from YAML).
    assert d.display_name in prompt
    assert d.authority.decides_alone[0] in prompt


def test_authority_is_rendered_from_yaml_not_the_template(roster: Any, composer: Any) -> None:
    """DoD + R5: editing the agent's authority list changes the rendered prompt
    with NO template edit — the same template, a different definition."""
    ceo = roster.get("ceo")
    base_prompt = composer.compose(ceo)
    assert "Feature prioritization" in base_prompt

    # Mutate ONLY the authority (frozen model -> a copy); keep the template.
    novel = "Approve interpretive dance performances"
    mutated = ceo.model_copy(
        update={
            "authority": Authority(
                decides_alone=(novel,),
                must_escalate=(Escalation(decision="Renaming the company", to=("human",)),),
            )
        }
    )
    new_prompt = composer.compose(mutated)
    assert novel in new_prompt  # the edited authority now appears…
    assert "Feature prioritization" not in new_prompt  # …and the old is gone
    assert "Renaming the company -> human" in new_prompt


def test_no_template_hardcodes_authority_text(roster: Any) -> None:
    """R5: no role template may contain its authority items as literal text —
    they must come only from the YAML-rendered base section."""
    for d in roster.all():
        template_src = (PROMPTS_DIR / d.system_prompt_template).read_text(encoding="utf-8")
        for item in d.authority.decides_alone:
            assert item not in template_src, f"{d.agent_id} template hard-codes authority: {item!r}"
        for esc in d.authority.must_escalate:
            assert esc.decision not in template_src, f"{d.agent_id} hard-codes escalation text"


def test_security_veto_and_cosign_render(roster: Any, composer: Any) -> None:
    assert "SECURITY VETO" in composer.compose(roster.get("security"))
    assert "co-sign" in composer.compose(roster.get("architect"))
    # A terminal-advisory agent (critic) renders the "nothing to escalate" branch.
    assert "terminal-advisory" in composer.compose(roster.get("critic"))


def test_memory_and_task_context_placeholders(roster: Any, composer: Any) -> None:
    empty = composer.compose(roster.get("qa"))
    assert "(no memory retrieved for this turn)" in empty
    assert "(no task/thread context for this turn)" in empty
    filled = composer.compose(roster.get("qa"), memory="MEM-BLOB", task_context="TASK-BLOB")
    assert "MEM-BLOB" in filled and "TASK-BLOB" in filled


def test_localization_sample_template_renders(composer: Any) -> None:
    """TITAN-AGT-006: the sample role composes from its YAML + template alone."""
    files = [*AGENTS_DIR.glob("*.yaml"), SAMPLE_LOCALIZATION]
    reg = build_registry(files)
    prompt = composer.compose(reg.get("localization"))
    assert all(i >= 0 for i in _section_indices(prompt))
    assert "Localization Engineer" in prompt
    assert "Translation-key" in prompt
