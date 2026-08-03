"""The evolution registry CI gate bites (Phase 83).

Green on the shipped tree; RED on drift, an unapproved champion entry, an orphan
prompt, and a missing core agent — proving the static half of TITAN-EVO-001/002
actually catches an un-pinned or unapproved prompt change.
"""

import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "check_evolution", REPO_ROOT / "apps" / "api" / "scripts" / "check_evolution.py"
)
assert _SPEC and _SPEC.loader
check_evolution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_evolution)

AGENTS_DIR = REPO_ROOT / "config" / "agents"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "evolution").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "evolution", root / "evolution", dirs_exist_ok=True)
    return root


def _errors(root: Path) -> list[str]:
    return check_evolution.collect_errors(root, agents_dir=AGENTS_DIR)


def test_shipped_tree_passes() -> None:
    assert check_evolution.collect_errors() == []


def test_clean_copy_passes(tree: Path) -> None:
    assert _errors(tree) == []


def test_drift_is_caught(tree: Path) -> None:
    f = tree / "evolution" / "prompts" / "reviewer" / "system.md"
    f.write_bytes(f.read_bytes() + b"\n{# unpinned edit #}\n")
    assert any("DRIFT" in e for e in _errors(tree))


def _mutate_manifest(root: Path, fn: Any) -> None:
    path = root / "evolution" / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_unapproved_entry_is_caught(tree: Path) -> None:
    _mutate_manifest(tree, lambda d: d["prompts"]["ceo"].update({"approved_by": None}))
    assert any("UNAPPROVED" in e for e in _errors(tree))


def test_missing_core_agent_is_caught(tree: Path) -> None:
    _mutate_manifest(tree, lambda d: d["prompts"].pop("architect"))
    # popping the manifest entry leaves architect/system.md as an orphan too.
    errs = _errors(tree)
    assert any("architect" in e and "no manifest prompt entry" in e for e in errs)


def test_orphan_prompt_is_caught(tree: Path) -> None:
    orphan = tree / "evolution" / "prompts" / "ghost" / "system.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\nid: x\n---\nbody\n", encoding="utf-8")
    assert any("unpinned file" in e for e in _errors(tree))


def test_fabricated_approver_is_caught(tree: Path) -> None:
    # Two arbitrary non-empty strings must NOT read as approved (HIGH-1 hardening).
    _mutate_manifest(tree, lambda d: d["prompts"]["ceo"].update({"approved_by": "somebody"}))
    assert any("valid user UUID" in e for e in _errors(tree))


def test_missing_frontmatter_field_is_caught(tree: Path) -> None:
    f = tree / "evolution" / "prompts" / "reviewer" / "system.md"
    # A prompt whose front-matter drops model_constraints must fail (TITAN-EVO-015).
    f.write_text(
        "---\nid: prompt.reviewer.system\nsemver: 1.0.0\nvariables: []\n"
        "eval_requirements:\n  tracks: []\n---\nbody\n",
        encoding="utf-8",
    )
    assert any("front-matter missing" in e and "model_constraints" in e for e in _errors(tree))


def test_ungated_inheritance_is_caught(tree: Path) -> None:
    f = tree / "evolution" / "prompts" / "qa" / "system.md"
    _, _, body = f.read_text(encoding="utf-8").partition("---\n")
    # Re-add valid front-matter but make the body extend an unpinned partial.
    f.write_text(
        "---\nid: prompt.qa.system\nsemver: 1.0.0\nvariables: []\nmodel_constraints: {}\n"
        'eval_requirements:\n  tracks: []\n---\n{% extends "evil/partial.md" %}\n',
        encoding="utf-8",
    )
    assert any("not the pinned base" in e for e in _errors(tree))
