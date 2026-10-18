"""Prompt registry + the EP approval gate (Phase 83, Vol 10 §2/§6.1).

Pure unit tests over the real ``/evolution`` registry and safe copies of it (no
DB, no Redis). They prove the two runtime-critical DoD properties:

  * an UNAPPROVED (or drifted) prompt version is never selected — the composer /
    resolver refuses it (TITAN-EVO-001);
  * a ``git revert`` of an evolution change restores the prior prompt bytes
    byte-for-byte, and the reverted (previously approved) version is served again
    (TITAN-EVO-002).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from titan.agents.loader import load_roster
from titan.agents.prompts import SECTION_MARKERS, PromptComposer
from titan.evolution.manifest import content_sha256, load_manifest
from titan.evolution.resolver import EvolutionError, EvolutionResolver

REPO_ROOT = Path(__file__).resolve().parents[3]
EVOLUTION_DIR = REPO_ROOT / "evolution"
PROMPTS_DIR = EVOLUTION_DIR / "prompts"
AGENTS_DIR = REPO_ROOT / "config" / "agents"

_CORE = (
    "ceo",
    "orchestrator",
    "pm_product",
    "pm_project",
    "architect",
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
)


# --- the real, shipped registry -------------------------------------------- #


def test_manifest_pins_every_core_agent() -> None:
    manifest = load_manifest(REPO_ROOT)
    for agent_id in _CORE:
        assert agent_id in manifest.prompts, f"{agent_id} missing from manifest"
        entry = manifest.prompts[agent_id]
        assert entry.approved, f"{agent_id} genesis entry must be approved"
        # The pinned sha256 matches the bytes on disk (no drift in the shipped tree).
        on_disk = content_sha256((REPO_ROOT / entry.path).read_bytes())
        assert on_disk == entry.sha256, f"{agent_id} manifest sha != on-disk bytes"


def test_resolver_serves_approved_prompt_sha_matching_manifest() -> None:
    resolver = EvolutionResolver.load(PROMPTS_DIR)
    manifest = resolver.manifest
    for agent_id in _CORE:
        resolved = resolver.resolve_prompt(agent_id)
        assert resolved.sha256 == manifest.prompts[agent_id].sha256
        assert resolved.template_name == f"{agent_id}/system.md"


def test_composer_renders_seven_sections_from_registry() -> None:
    roster = load_roster(AGENTS_DIR)
    composer = PromptComposer(EvolutionResolver.load(PROMPTS_DIR))
    ceo = roster.get("ceo")
    assert ceo is not None
    prompt = composer.compose(ceo, task_context="Build X", memory="(mem)")
    idx = [prompt.find(m) for m in SECTION_MARKERS]
    assert all(i >= 0 for i in idx) and idx == sorted(idx)
    assert composer.prompt_sha(ceo) == resolver_sha("ceo")


def resolver_sha(agent_id: str) -> str:
    return EvolutionResolver.load(PROMPTS_DIR).resolve_prompt(agent_id).sha256


# --- the gate, on a safe copy of the registry ------------------------------ #


@pytest.fixture
def registry_copy(tmp_path: Path) -> Path:
    """A throwaway copy of /evolution so gate tests can mutate freely."""
    dst = tmp_path / "evolution"
    shutil.copytree(EVOLUTION_DIR, dst)
    return dst


def _rewrite_manifest(evo_dir: Path, mutate: Any) -> None:
    path = evo_dir / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


def test_unapproved_prompt_is_refused(registry_copy: Path) -> None:
    """DoD: an unapproved prompt version is never selected at runtime."""

    def blank_approval(data: dict[str, Any]) -> None:
        data["prompts"]["reviewer"]["approved_by"] = None
        data["prompts"]["reviewer"]["approved_at"] = None

    _rewrite_manifest(registry_copy, blank_approval)
    resolver = EvolutionResolver.load(registry_copy / "prompts")
    with pytest.raises(EvolutionError, match="not approved"):
        resolver.resolve_prompt("reviewer")
    # A different, still-approved agent is unaffected.
    assert resolver.resolve_prompt("ceo").sha256


def test_composer_refuses_to_render_unapproved(registry_copy: Path) -> None:
    """The refusal reaches the runtime path: the composer raises rather than
    serving an unapproved prompt (the orchestrator refuses the artifact)."""

    def blank_approval(data: dict[str, Any]) -> None:
        data["prompts"]["ceo"]["approved_by"] = None
        data["prompts"]["ceo"]["approved_at"] = None

    _rewrite_manifest(registry_copy, blank_approval)
    composer = PromptComposer(EvolutionResolver.load(registry_copy / "prompts"))
    ceo = load_roster(AGENTS_DIR).get("ceo")
    assert ceo is not None
    with pytest.raises(EvolutionError):
        composer.compose(ceo)


def test_drifted_prompt_is_refused(registry_copy: Path) -> None:
    """An on-disk edit that the manifest has not approved is refused (the hash
    no longer matches the pinned, approved sha256)."""
    target = registry_copy / "prompts" / "qa" / "system.md"
    target.write_bytes(target.read_bytes() + b"\n{# sneaky unapproved edit #}\n")
    resolver = EvolutionResolver.load(registry_copy / "prompts")
    with pytest.raises(EvolutionError, match="unapproved change"):
        resolver.resolve_prompt("qa")


def test_unapproved_base_refuses_all_prompts(registry_copy: Path) -> None:
    """The shared base is part of every rendered prompt, so drifting it refuses
    every agent — the gate cannot be bypassed via the base."""
    base = registry_copy / "prompts" / "_base" / "system.md"
    base.write_bytes(base.read_bytes() + b"\n{# base drift #}\n")
    resolver = EvolutionResolver.load(registry_copy / "prompts")
    with pytest.raises(EvolutionError):
        resolver.resolve_prompt("reviewer")


def test_git_revert_restores_prior_bytes_byte_for_byte(registry_copy: Path) -> None:
    """DoD: revert of an evolution change restores the prior prompt bytes
    byte-for-byte and the reverted (approved) version is served again."""
    target = registry_copy / "prompts" / "security" / "system.md"
    original = target.read_bytes()
    original_sha = (
        EvolutionResolver.load(registry_copy / "prompts").resolve_prompt("security").sha256
    )

    # A change lands on disk without approval → refused (never served).
    target.write_bytes(original + b"\n{# proposed v2, not yet approved #}\n")
    drifted = EvolutionResolver.load(registry_copy / "prompts")
    with pytest.raises(EvolutionError):
        drifted.resolve_prompt("security")

    # `git revert` = the bytes come back. The reverted version is approved again.
    target.write_bytes(original)
    assert target.read_bytes() == original  # byte-for-byte
    reverted = EvolutionResolver.load(registry_copy / "prompts")
    assert reverted.resolve_prompt("security").sha256 == original_sha


def test_reload_picks_up_a_manifest_change(registry_copy: Path) -> None:
    resolver = EvolutionResolver.load(registry_copy / "prompts")
    assert resolver.resolve_prompt("docs")  # approved at load

    def blank(data: dict[str, Any]) -> None:
        data["prompts"]["docs"]["approved_by"] = None

    _rewrite_manifest(registry_copy, blank)
    resolver.reload()
    with pytest.raises(EvolutionError):
        resolver.resolve_prompt("docs")
