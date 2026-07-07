"""Phase 26 — AgentDefinition schema, 16-agent roster & registry (Vol 2 §2.1-§4).

Three layers:
  * schema strictness — every §2.1 field required, extras forbidden, tiers bounded
    (TITAN-AGT-001: "a missing field fails");
  * the real roster — the 16 shipped YAMLs load, and a snapshot pins each agent's
    decides_alone / must_escalate + the veto / co-sign / fail-open-closed rules
    (TITAN-AGT-008, TITAN-AGT-011);
  * fail-loud loader + extensibility — a malformed/incomplete roster refuses to
    boot, and a brand-new Localization agent loads from YAML alone, zero core
    diffs (TITAN-AGT-006).

The schema/loader layers need no database; the HTTP layer uses the `api` fixture.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from titan.agents.definition import CORE_ROSTER, AgentDefinition
from titan.agents.loader import (
    RosterError,
    load_roster,
    resolve_config_dir,
)

# apps/api/tests/<this> → parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "config" / "agents"
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
SAMPLE_LOCALIZATION = AGENTS_DIR / "samples" / "localization.yaml"


# --------------------------------------------------------------------------- #
# Schema strictness (unit — no I/O)                                            #
# --------------------------------------------------------------------------- #

_VALID: dict[str, Any] = {
    "agent_id": "sample",
    "display_name": "Sample",
    "tier": 3,
    "supervisor": "pm_project",
    "system_prompt_template": "agents/sample.j2",
    "authority": {
        "decides_alone": ["thing one"],
        "must_escalate": [{"decision": "big thing", "to": ["ceo"]}],
    },
    "memory_scopes": {"read": ["short_term", "project"], "write": ["short_term"]},
    "tool_grants": ["memory.search", "messages.send"],
    "model_policy": {"tier": "standard", "temperature": 0.3, "max_output_tokens": 4096},
    "kpis": ["some_metric"],
    "failure_policy": {"mode": "default"},
}

_REQUIRED_TOP_FIELDS = [
    "agent_id",
    "display_name",
    "tier",
    "supervisor",
    "system_prompt_template",
    "authority",
    "memory_scopes",
    "tool_grants",
    "model_policy",
    "kpis",
    "failure_policy",
]


def test_valid_definition_parses() -> None:
    d = AgentDefinition.model_validate(_VALID)
    assert d.agent_id == "sample"
    assert d.authority.must_escalate[0].to == ("ceo",)
    assert d.model_policy.max_output_tokens == 4096


@pytest.mark.parametrize("field", _REQUIRED_TOP_FIELDS)
def test_each_required_field_is_mandatory(field: str) -> None:
    """TITAN-AGT-001 acceptance: deleting any of the eight §2.1 field-groups fails."""
    broken = {k: v for k, v in _VALID.items() if k != field}
    with pytest.raises(ValueError):
        AgentDefinition.model_validate(broken)


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValueError):
        AgentDefinition.model_validate({**_VALID, "surprise": 1})


@pytest.mark.parametrize("tier", [0, 1, 2, 3, "cross"])
def test_tier_accepts_valid(tier: Any) -> None:
    assert AgentDefinition.model_validate({**_VALID, "tier": tier}).tier == tier


@pytest.mark.parametrize("tier", [4, -1, "boss", 1.5])
def test_tier_rejects_invalid(tier: Any) -> None:
    with pytest.raises(ValueError):
        AgentDefinition.model_validate({**_VALID, "tier": tier})


def test_escalation_requires_a_target() -> None:
    bad = {
        **_VALID,
        "authority": {"decides_alone": [], "must_escalate": [{"decision": "x", "to": []}]},
    }
    with pytest.raises(ValueError):
        AgentDefinition.model_validate(bad)


def test_model_temperature_bounded() -> None:
    bad = {**_VALID, "model_policy": {"tier": "fast", "temperature": 9.0, "max_output_tokens": 10}}
    with pytest.raises(ValueError):
        AgentDefinition.model_validate(bad)


def test_bad_agent_id_pattern_rejected() -> None:
    with pytest.raises(ValueError):
        AgentDefinition.model_validate({**_VALID, "agent_id": "Not-An-Id"})


def test_definition_is_immutable() -> None:
    d = AgentDefinition.model_validate(_VALID)
    with pytest.raises(ValueError):
        d.agent_id = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# The real shipped roster                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def roster() -> Any:
    return load_roster(AGENTS_DIR)


def test_core_roster_loads_16(roster: Any) -> None:
    assert len(roster) == 16
    assert roster.ids() == CORE_ROSTER


def test_relative_default_path_resolves_from_cwd() -> None:
    """The Settings default 'config/agents' resolves via upward search (pytest
    runs from apps/api), so lifespan finds the roster with no absolute path."""
    reg = load_roster("config/agents")
    assert len(reg) == 16


def test_resolve_absolute_path_passthrough() -> None:
    assert resolve_config_dir(AGENTS_DIR) == AGENTS_DIR


def test_resolve_stops_at_repo_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upward search must not escape the repo: a stray ancestor config/agents
    ABOVE the repo root is never picked up (confirmed review finding)."""
    stray = tmp_path / "config" / "agents"  # the tempting out-of-project match
    stray.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # repo-root marker (no config/agents here)
    deep = repo / "apps" / "api"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    resolved = resolve_config_dir("config/agents")
    # Stopped at the repo boundary -> returns the in-repo (non-existent) path,
    # NOT the stray ancestor. load_roster would then fail loud with that path.
    assert resolved == deep / "config" / "agents"
    assert resolved != stray


EXPECTED_SUPERVISOR = {
    "ceo": "human",
    "orchestrator": "ceo",
    "pm_product": "ceo",
    "architect": "ceo",
    "pm_project": "orchestrator",
    "eng_backend": "pm_project",
    "eng_frontend": "pm_project",
    "eng_database": "pm_project",
    "eng_devops": "pm_project",
    "qa": "pm_project",
    "security": "pm_project",
    "docs": "pm_project",
    "reviewer": "architect",
    "critic": "architect",
    "research": "orchestrator",
    "memory": "orchestrator",
}

EXPECTED_TIER = {
    "ceo": 0,
    "orchestrator": 1,
    "pm_product": 1,
    "architect": 1,
    "pm_project": 2,
    "eng_backend": 3,
    "eng_frontend": 3,
    "eng_database": 3,
    "eng_devops": 3,
    "qa": 3,
    "security": 3,
    "docs": 3,
    "reviewer": "cross",
    "critic": "cross",
    "research": "cross",
    "memory": "cross",
}

# §8 model-policy.yaml agent → tier mapping.
EXPECTED_MODEL_TIER = {
    "ceo": "reasoning",
    "architect": "reasoning",
    "critic": "reasoning",
    "security": "reasoning",
    "eng_backend": "standard",
    "eng_frontend": "standard",
    "eng_database": "standard",
    "eng_devops": "standard",
    "reviewer": "standard",
    "qa": "standard",
    "pm_product": "standard",
    "orchestrator": "standard",
    "pm_project": "fast",
    "docs": "fast",
    "research": "fast",
    "memory": "fast",
}


def test_supervisors_match_roster(roster: Any) -> None:
    assert {d.agent_id: d.supervisor for d in roster.all()} == EXPECTED_SUPERVISOR


def test_tiers_match_roster(roster: Any) -> None:
    assert {d.agent_id: d.tier for d in roster.all()} == EXPECTED_TIER


def test_model_tiers_match_section8(roster: Any) -> None:
    assert {d.agent_id: d.model_policy.tier for d in roster.all()} == EXPECTED_MODEL_TIER


# The DoD snapshot: each agent's authority, verbatim from §4.1-§4.16.
# must_escalate rows are (decision, targets, co_sign).
EXPECTED_DECIDES_ALONE = {
    "ceo": [
        "Feature prioritization",
        "Scope cuts within stated budget",
        "Release go/no-go",
        "Accepting PM roadmaps",
    ],
    "orchestrator": [
        "Task ordering",
        "Parallel-vs-serial execution",
        "Retry/reassign of blocked tasks",
        "Invoking Research or Critic",
    ],
    "pm_product": [
        "Story slicing",
        "Acceptance-criteria wording",
        "Backlog ordering within CEO priorities",
    ],
    "architect": [
        "Internal component design",
        "Patterns (repository, CQRS where justified)",
        "Library selection within the canonical stack",
        "ADR acceptance",
    ],
    "pm_project": [
        "Task assignment and reassignment",
        "Estimates",
        "Task reopening",
        "Intra-sprint reordering",
    ],
    "eng_backend": [
        "Implementation details within the API contract",
        "Internal refactors",
        "Library helpers already approved in code_patterns",
    ],
    "eng_frontend": [
        "Component structure",
        "Styling within the design language",
        "State-management wiring within Architect-approved patterns",
    ],
    "eng_database": [
        "Index and constraint choices",
        "Migration mechanics",
        "Query rewrites",
    ],
    "eng_devops": [
        "Pipeline structure",
        "Image optimization",
        "Alert thresholds within SLOs",
    ],
    "qa": [
        "Test design",
        "Pass/fail verdicts against written acceptance criteria",
        "Regression-suite membership",
    ],
    "security": [
        "Vulnerability severity ratings",
        "Security veto — blocking any merge/release with a Critical/High finding (§7.4)",
    ],
    "reviewer": [
        "Approve/request-changes verdicts",
        "Style and standards rulings (Volume 8 is binding precedent)",
    ],
    "docs": [
        "Document structure",
        "Wording",
        "Regeneration scheduling",
    ],
    "research": [
        "Source selection",
        "Brief structure",
        "Confidence ratings",
    ],
    "memory": [
        "Curation verdicts",
        "Pruning within retention policy",
        "Embedding re-index scheduling",
    ],
    "critic": [
        "Critique content and severity labels (advisory only — the Critic never "
        "blocks; owners and gatekeepers decide)",
    ],
}

EXPECTED_MUST_ESCALATE: dict[str, list[tuple[str, tuple[str, ...], bool]]] = {
    "ceo": [
        ("Budget/cost-ceiling changes", ("human",), False),
        ("Material scope expansion", ("human",), False),
        ("Legal/compliance-sensitive features", ("human",), False),
        ("Security-veto overrides (§7.4)", ("human",), False),
        ("Project cancellation", ("human",), False),
    ],
    "orchestrator": [
        ("Milestone changes", ("ceo",), False),
        ("Architectural sequencing disputes", ("architect",), False),
        ("Resource exhaustion such as token-budget overrun", ("ceo",), False),
    ],
    "pm_product": [
        ("New scope discovered during grooming", ("ceo",), False),
        ("Technically infeasible stories flagged by Architect", ("conflict_resolution",), False),
    ],
    "architect": [
        ("Any deviation from the canonical stack", ("ceo", "human"), False),
        ("Cost-bearing infrastructure choices", ("ceo",), False),
        ("Security-relevant design", ("security",), True),
    ],
    "pm_project": [
        ("Sprint-goal risk", ("orchestrator",), False),
        (
            "Repeated failure by one agent on one task (>= 3 cycles, rule §7.5)",
            ("orchestrator",),
            False,
        ),
        ("Inter-agent conflicts it cannot settle in one round", ("conflict_resolution",), False),
    ],
    "eng_backend": [
        ("Contract changes", ("architect",), False),
        ("New dependencies", ("architect", "security"), True),
        ("Performance targets unmet after 2 attempts", ("pm_project",), False),
    ],
    "eng_frontend": [
        ("API-contract gaps", ("eng_backend",), False),
        ("Design-language changes", ("architect",), False),
        ("Accessibility conflicts with requested features", ("pm_product",), False),
    ],
    "eng_database": [
        ("Schema changes that break API contracts", ("architect",), False),
        ("Destructive migrations", ("pm_project", "eng_devops"), True),
        ("Storage-engine choices beyond the canonical stack", ("architect",), False),
    ],
    "eng_devops": [
        ("Production topology changes", ("architect",), False),
        ("Anything cost-bearing", ("ceo",), False),
        ("Secret-handling design", ("security",), True),
    ],
    "qa": [
        ("Ambiguous acceptance criteria", ("pm_product",), False),
        ("Release-blocking defect disputes", ("conflict_resolution",), False),
    ],
    "security": [
        ("Veto overrides (only the human client can override, via CEO)", ("ceo", "human"), False),
        ("Novel threat classes needing research", ("research",), False),
        ("Compliance interpretations", ("ceo", "human"), False),
    ],
    "reviewer": [
        ("Architectural concerns discovered in review", ("architect",), False),
        ("Author disputes after 2 review cycles", ("conflict_resolution",), False),
    ],
    "docs": [
        ("Contradictions between code and ADRs", ("architect",), False),
        ("Missing information it cannot derive", ("owning_agent",), False),
    ],
    "research": [
        ("Findings that contradict current architecture", ("architect",), False),
        ("Paywalled/unavailable sources", ("requester",), False),
    ],
    "memory": [
        ("Retention-policy changes", ("ceo",), False),
        (
            "Deleting decision history (never allowed — flag instead; DecisionLogs are "
            "append-only)",
            ("orchestrator",),
            False,
        ),
    ],
    "critic": [],
}


@pytest.mark.parametrize("agent_id", sorted(CORE_ROSTER))
def test_snapshot_decides_alone(roster: Any, agent_id: str) -> None:
    d = roster.get(agent_id)
    assert list(d.authority.decides_alone) == EXPECTED_DECIDES_ALONE[agent_id]


@pytest.mark.parametrize("agent_id", sorted(CORE_ROSTER))
def test_snapshot_must_escalate(roster: Any, agent_id: str) -> None:
    d = roster.get(agent_id)
    got = [(e.decision, e.to, e.co_sign) for e in d.authority.must_escalate]
    assert got == EXPECTED_MUST_ESCALATE[agent_id]


# TITAN-AGT-011: veto / co-sign / fail-open-closed encoded + mechanically checkable.


def test_security_holds_the_only_veto(roster: Any) -> None:
    veto_holders = {d.agent_id for d in roster.all() if d.authority.veto}
    assert veto_holders == {"security"}


def test_cosign_escalations(roster: Any) -> None:
    cosigned = {
        (d.agent_id, e.decision): e.to
        for d in roster.all()
        for e in d.authority.must_escalate
        if e.co_sign
    }
    assert cosigned == {
        ("architect", "Security-relevant design"): ("security",),
        ("eng_backend", "New dependencies"): ("architect", "security"),
        ("eng_database", "Destructive migrations"): ("pm_project", "eng_devops"),
        ("eng_devops", "Secret-handling design"): ("security",),
    }


def test_fail_open_and_fail_closed_gates(roster: Any) -> None:
    modes = {d.agent_id: d.failure_policy.mode for d in roster.all()}
    assert modes["security"] == "fail_closed"  # TITAN-AGT-010
    assert modes["reviewer"] == "fail_closed"
    assert modes["docs"] == "fail_open"
    assert modes["critic"] == "fail_open"
    # everyone else follows the default ladder
    defaulted = {a for a, m in modes.items() if m == "default"}
    assert defaulted == CORE_ROSTER - {"security", "reviewer", "docs", "critic"}


def test_ceo_escalates_to_human_orchestrator_has_watchdog(roster: Any) -> None:
    assert roster.get("ceo").failure_policy.escalates_to_human is True
    assert roster.get("orchestrator").failure_policy.watchdog_restart is True
    # TITAN-AGT-009 belongs to the orchestrator alone
    watchdogs = {d.agent_id for d in roster.all() if d.failure_policy.watchdog_restart}
    assert watchdogs == {"orchestrator"}


def test_database_destructive_ops_skip_retries(roster: Any) -> None:
    assert roster.get("eng_database").failure_policy.skip_retries_on == ("Destructive migrations",)


def test_memory_agent_reads_and_writes_all_scopes(roster: Any) -> None:
    d = roster.get("memory")
    all_scopes = {
        "short_term",
        "project",
        "organization",
        "code_patterns",
        "architecture",
        "lessons_learned",
    }
    assert set(d.memory_scopes.read) == all_scopes
    assert set(d.memory_scopes.write) == all_scopes


def test_universal_tools_and_memory_defaults(roster: Any) -> None:
    """§2.2 shared defaults hold for every agent: the three universal tools, and
    write access to short_term + lessons_learned."""
    for d in roster.all():
        assert {"memory.search", "memory.write_note", "messages.send"} <= set(d.tool_grants)
        assert {"short_term", "lessons_learned"} <= set(d.memory_scopes.write)
        assert {"short_term", "project"} <= set(d.memory_scopes.read)


def test_ceo_is_sole_client_contact(roster: Any) -> None:
    """§4.1: the CEO is the only agent granted client.message."""
    contacts = {d.agent_id for d in roster.all() if "client.message" in d.tool_grants}
    assert contacts == {"ceo"}


def test_every_agent_prompt_template_stub_exists(roster: Any) -> None:
    """Each system_prompt_template reference resolves to a stub on disk (the
    scaffold Phase 28 composes)."""
    for d in roster.all():
        assert (PROMPTS_DIR / d.system_prompt_template).is_file(), d.agent_id


# --------------------------------------------------------------------------- #
# Fail-loud loader — a bad roster refuses to boot                             #
# --------------------------------------------------------------------------- #


def _copy_core(tmp_path: Path) -> Path:
    dst = tmp_path / "agents"
    dst.mkdir()
    for src in AGENTS_DIR.glob("*.yaml"):
        shutil.copy(src, dst / src.name)
    return dst


def _rewrite(path: Path, mutate: Any) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_empty_dir_refuses(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    with pytest.raises(RosterError, match="no agent YAML"):
        load_roster(tmp_path / "agents")


def test_missing_dir_refuses(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="not found"):
        load_roster(tmp_path / "nope")


def test_incomplete_roster_refuses(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    (d / "qa.yaml").unlink()
    with pytest.raises(RosterError, match="missing required core agents: qa"):
        load_roster(d)


def test_deleting_a_required_field_refuses(tmp_path: Path) -> None:
    """DoD: deleting a required field from any YAML fails validation at load."""
    d = _copy_core(tmp_path)
    _rewrite(d / "architect.yaml", lambda data: data.pop("authority"))
    with pytest.raises(RosterError, match="architect.yaml"):
        load_roster(d)


def test_dangling_supervisor_refuses(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    _rewrite(d / "qa.yaml", lambda data: data.__setitem__("supervisor", "nobody"))
    with pytest.raises(RosterError, match="supervisor 'nobody'"):
        load_roster(d)


def test_self_supervisor_refuses(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    _rewrite(d / "qa.yaml", lambda data: data.__setitem__("supervisor", "qa"))
    with pytest.raises(RosterError, match="own supervisor"):
        load_roster(d)


def test_unknown_escalation_target_refuses(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["authority"]["must_escalate"][0]["to"] = ["nowhere"]

    _rewrite(d / "reviewer.yaml", mutate)
    with pytest.raises(RosterError, match="target 'nowhere'"):
        load_roster(d)


def test_filename_id_mismatch_refuses(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    _rewrite(d / "qa.yaml", lambda data: data.__setitem__("agent_id", "quality"))
    with pytest.raises(RosterError, match="does not match filename"):
        load_roster(d)


# --------------------------------------------------------------------------- #
# TITAN-AGT-006 extensibility — a new agent is YAML-only                       #
# --------------------------------------------------------------------------- #


def test_localization_sample_is_a_valid_definition() -> None:
    data = yaml.safe_load(SAMPLE_LOCALIZATION.read_text(encoding="utf-8"))
    d = AgentDefinition.model_validate(data)
    assert d.agent_id == "localization"
    assert d.supervisor == "pm_project"


def test_adding_localization_needs_only_yaml(tmp_path: Path) -> None:
    """TITAN-AGT-006 acceptance: drop the sample YAML alongside the core roster
    and it appears in the registry — no schema/loader/orchestrator change."""
    d = _copy_core(tmp_path)
    shutil.copy(SAMPLE_LOCALIZATION, d / "localization.yaml")
    reg = load_roster(d)  # same loader, unchanged
    assert len(reg) == 17
    assert "localization" in reg
    loc = reg.get("localization")
    assert loc is not None
    assert loc.display_name == "Localization Engineer"
    assert loc.supervisor in reg  # its supervisor resolves within the roster


def test_sample_localization_not_in_default_roster() -> None:
    """The sample lives under samples/ so the non-recursive default scan ignores
    it — the shipped roster stays exactly 16 (TITAN-AGT-008)."""
    reg = load_roster(AGENTS_DIR)
    assert "localization" not in reg


def test_build_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    dupe = d / "qa2.yaml"
    _rewrite_copy = yaml.safe_load((d / "qa.yaml").read_text(encoding="utf-8"))
    dupe.write_text(yaml.safe_dump(_rewrite_copy, allow_unicode=True), encoding="utf-8")
    # qa2.yaml has agent_id 'qa' -> filename mismatch is caught first, which is
    # itself the duplicate-prevention guarantee.
    with pytest.raises(RosterError):
        load_roster(d)


# --------------------------------------------------------------------------- #
# HTTP surface — the roster over the existing agents API (needs the DB)        #
# --------------------------------------------------------------------------- #


@pytest.mark.schema
def test_list_agent_definitions_endpoint(api: dict[str, Any]) -> None:
    resp = api["client"].get("/api/v1/agent-definitions", headers=api["token_for"]("viewer"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 16
    by_id = {d["agent_id"]: d for d in body}
    assert set(by_id) == CORE_ROSTER
    ceo = by_id["ceo"]
    assert ceo["tier"] == 0
    assert ceo["supervisor"] == "human"
    assert ceo["authority"]["must_escalate"][0]["to"] == ["human"]
    # first entry is the top of the org (deterministic tier-then-id order)
    assert body[0]["agent_id"] == "ceo"


@pytest.mark.schema
def test_get_one_agent_definition_endpoint(api: dict[str, Any]) -> None:
    resp = api["client"].get(
        "/api/v1/agent-definitions/security", headers=api["token_for"]("viewer")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "security"
    assert body["authority"]["veto"] is True
    assert body["failure_policy"]["mode"] == "fail_closed"


@pytest.mark.schema
def test_unknown_agent_definition_404(api: dict[str, Any]) -> None:
    resp = api["client"].get("/api/v1/agent-definitions/nope", headers=api["token_for"]("viewer"))
    assert resp.status_code == 404


@pytest.mark.schema
def test_agent_definitions_require_auth(api: dict[str, Any]) -> None:
    assert api["client"].get("/api/v1/agent-definitions").status_code == 401
    assert api["client"].get("/api/v1/agent-definitions/ceo").status_code == 401
