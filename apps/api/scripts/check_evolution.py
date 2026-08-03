"""CI gate: the evolution prompt registry is intact, pinned, and approved
(Phase 83, Vol 10 §2/§6.1). Fails the build (exit 1) on any drift between the
SHA-pinned manifest and the on-disk artifacts, an unapproved champion entry, a
core agent missing from the registry, an orphan prompt file, or malformed
front-matter.

This is the static half of TITAN-EVO-001/002: it guarantees that what ships on
the main branch is exactly the approved, content-addressed set the runtime
resolver will serve — a prompt edit that skips the manifest (and therefore the EP
approval) can never merge silently.

Pure, testable core (:func:`collect_errors`) + thin :func:`main`.
"""

import hashlib
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# {% extends "x" %} / {% include "x" %} — only the pinned base may be referenced.
_INHERIT_RE = re.compile(r"{%-?\s*(extends|include)\s+[\"']([^\"']+)[\"']")
_ALLOWED_INHERIT = "_base/system.md"

REPO_ROOT = Path(__file__).resolve().parents[3]
EVOLUTION_DIR = REPO_ROOT / "evolution"
MANIFEST = EVOLUTION_DIR / "manifest.yaml"
PROMPTS_DIR = EVOLUTION_DIR / "prompts"
AGENTS_DIR = REPO_ROOT / "config" / "agents"

# Every prompt front-matter must declare these (TITAN-EVO-015). model_constraints
# and eval_requirements are enforced so a prompt can never silently drop them.
_FM_REQUIRED = ("id", "semver", "variables", "model_constraints", "eval_requirements")


def _valid_approver(value: Any) -> bool:
    """A real approver id, not just any non-empty string (closes the CI blind
    spot where two arbitrary strings would read as 'approved')."""
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        return False
    return True


def _valid_timestamp(value: Any) -> bool:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return True


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if end is None:
        return {}, text
    meta = yaml.safe_load("".join(lines[1:end])) or {}
    return (meta if isinstance(meta, dict) else {}), "".join(lines[end + 1 :])


def _core_agent_ids(agents_dir: Path) -> set[str]:
    """The 16 core roster agents = config/agents/*.yaml (samples/ excluded)."""
    return {p.stem for p in agents_dir.glob("*.yaml")}


def _check_entry(errors: list[str], label: str, entry: dict[str, Any], root: Path) -> None:
    path = entry.get("path")
    sha = entry.get("sha256")
    if not path or not sha:
        errors.append(f"{label}: manifest entry missing path/sha256")
        return
    fpath = root / str(path)
    if not fpath.exists():
        errors.append(f"{label}: artifact file missing: {path}")
        return
    actual = _sha256(fpath)
    if actual != sha:
        errors.append(
            f"{label}: DRIFT — on-disk sha256 {actual[:12]} != manifest {str(sha)[:12]} "
            f"({path}); re-pin + re-approve via an EP"
        )
    if not (entry.get("approved_by") and entry.get("approved_at")):
        errors.append(
            f"{label}: UNAPPROVED — champion manifest entry lacks approved_by/approved_at "
            f"(TITAN-EVO-001)"
        )
    else:
        # Authenticity, not just presence: a fabricated approval of two arbitrary
        # strings must not read as approved (defence beyond branch protection).
        if not _valid_approver(entry.get("approved_by")):
            errors.append(f"{label}: approved_by is not a valid user UUID (TITAN-EVO-001)")
        if not _valid_timestamp(entry.get("approved_at")):
            errors.append(f"{label}: approved_at is not a valid ISO-8601 timestamp")


def collect_errors(root: Path = REPO_ROOT, *, agents_dir: Path = AGENTS_DIR) -> list[str]:
    errors: list[str] = []
    manifest_path = root / "evolution" / "manifest.yaml"
    prompts_dir = root / "evolution" / "prompts"
    if not manifest_path.exists():
        return [f"evolution manifest not found: {manifest_path}"]
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"manifest is not valid YAML: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest root must be a mapping"]

    for required in ("base", "routing", "prompts"):
        if required not in manifest:
            errors.append(f"manifest missing required section: {required}")
    prompts = manifest.get("prompts")
    if not isinstance(prompts, dict):
        errors.append("manifest 'prompts' must be a mapping")
        prompts = {}

    if isinstance(manifest.get("base"), dict):
        _check_entry(errors, "base", manifest["base"], root)
    if isinstance(manifest.get("routing"), dict):
        _check_entry(errors, "routing", manifest["routing"], root)
    if isinstance(manifest.get("scoring"), dict):
        _check_entry(errors, "scoring", manifest["scoring"], root)
    if isinstance(manifest.get("taxonomy"), dict):
        _check_entry(errors, "taxonomy", manifest["taxonomy"], root)

    pinned_paths: set[str] = set()
    for agent_id, entry in prompts.items():
        if not isinstance(entry, dict):
            errors.append(f"prompt '{agent_id}': entry must be a mapping")
            continue
        _check_entry(errors, f"prompt '{agent_id}'", entry, root)
        if entry.get("path"):
            pinned_paths.add(str(entry["path"]))
            # Front-matter must declare id/semver/variables (TITAN-EVO-015).
            fpath = root / str(entry["path"])
            if fpath.exists():
                meta, body = _split_front_matter(fpath.read_text(encoding="utf-8"))
                missing = [k for k in _FM_REQUIRED if k not in meta]
                if missing:
                    errors.append(f"prompt '{agent_id}': front-matter missing {missing}")
                # A role template may only extend/include the pinned base — an
                # unpinned partial would render ungated content (LOW-7 hardening).
                for tag, ref in _INHERIT_RE.findall(body):
                    if ref != _ALLOWED_INHERIT:
                        errors.append(
                            f"prompt '{agent_id}': {tag} '{ref}' is not the pinned base "
                            f"'{_ALLOWED_INHERIT}' — ungated template inheritance"
                        )

    # Every core roster agent must be pinned in the registry.
    for agent_id in sorted(_core_agent_ids(agents_dir)):
        if agent_id not in prompts:
            errors.append(f"core agent '{agent_id}' has no manifest prompt entry")

    # No orphan prompt files (a prompt on disk that the manifest does not pin).
    base_path = (
        str(manifest.get("base", {}).get("path", ""))
        if isinstance(manifest.get("base"), dict)
        else ""
    )
    for f in prompts_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if rel not in pinned_paths and rel != base_path:
            errors.append(f"unpinned file in prompt registry: {rel}")

    return errors


def main() -> None:
    errors = collect_errors()
    if errors:
        print("evolution registry check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)
    print("evolution registry OK: manifest pinned, approved, and drift-free")


if __name__ == "__main__":
    main()
