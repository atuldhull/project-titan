"""The pinned evolution manifest + prompt front-matter (Vol 10 §2/§6.1).

``/evolution/manifest.yaml`` is the single, git-versioned declaration of the
approved evolution champion (TITAN-EVO-002). Each entry pins a repo-relative
``path``, the content ``sha256`` (the *prompt SHA* logged on every LLM span,
TITAN-EVO-015), a ``semver``, the approving ``ep``, and — the human gate
(TITAN-EVO-001) — the ``approved_by`` user id and signed ``approved_at``. The
manifest changes ONLY through a pull request against ``/evolution/**``
(ADR-EVO-01); a ``git revert`` of such a change restores prior bytes byte-for-byte.

This module is pure (stdlib + PyYAML): it parses the manifest and prompt
front-matter and hashes content. It never touches the database or a domain module,
so the prompt composer can depend on it without widening the dependency graph.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

# Repo-relative anchors. Kept here so the loader, the CI drift gate, and the seed
# all agree on where artifacts live.
MANIFEST_REL = "evolution/manifest.yaml"
PROMPTS_REL = "evolution/prompts"

# Artifact kinds recorded in the manifest and the DB ledger. Extended by later
# phases (scoring weights, benchmark defs, agent specs) through the same EP flow.
KIND_PROMPT = "prompt"
KIND_PROMPT_BASE = "prompt_base"
KIND_ROUTING = "routing"


class ManifestError(ValueError):
    """The evolution manifest is malformed — refuse to resolve any artifact."""


def content_sha256(data: bytes) -> str:
    """The content hash that IS an artifact's identity (its prompt SHA)."""
    return hashlib.sha256(data).hexdigest()


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---\\n<yaml>\\n---\\n<body>`` into ``(metadata, body)``.

    A file with no leading ``---`` fence (or an unterminated one) returns
    ``({}, text)`` — the whole file is the body. Used both to read a prompt's
    declared metadata (id/semver/variables/model_constraints/eval_requirements)
    and, by the Jinja loader, to strip the fence before rendering.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening fence; find the closing fence line.
    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    meta = yaml.safe_load(fm_text) if fm_text.strip() else {}
    if not isinstance(meta, dict):
        raise ManifestError("prompt front-matter must be a YAML mapping")
    return meta, body


@dataclass(frozen=True)
class ArtifactEntry:
    """One pinned, approved artifact reference — never any prompt text."""

    kind: str
    ref: str  # agent_id for prompts; 'base' / 'routing' otherwise
    path: str  # repo-relative, POSIX
    sha256: str
    semver: str
    ep: str
    approved_by: str
    approved_at: str
    diff_url: str = ""

    @property
    def approved(self) -> bool:
        """The EP gate (TITAN-EVO-001): a real approver id AND a signed timestamp."""
        return bool(self.approved_by.strip()) and bool(self.approved_at.strip())

    def template_name(self) -> str:
        """The prompt's name relative to the prompts dir, e.g. ``reviewer/system.md``
        — what the composer's Jinja loader resolves. Defensive: an artifact whose
        path is not under the prompts dir (routing/scoring) has no template name, so
        fall back to its full path rather than raising for a non-prompt caller."""
        p = PurePosixPath(self.path)
        try:
            return p.relative_to(PROMPTS_REL).as_posix()
        except ValueError:
            return p.as_posix()


@dataclass(frozen=True)
class Manifest:
    version: int
    base: ArtifactEntry
    routing: ArtifactEntry
    prompts: dict[str, ArtifactEntry]

    def all_entries(self) -> list[ArtifactEntry]:
        return [self.base, self.routing, *self.prompts.values()]


def _entry(kind: str, ref: str, raw: dict[str, Any]) -> ArtifactEntry:
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest entry '{ref}' must be a mapping")
    try:
        path = str(raw["path"])
        sha = str(raw["sha256"])
    except KeyError as exc:
        raise ManifestError(f"manifest entry '{ref}' missing required key {exc}") from exc
    return ArtifactEntry(
        kind=kind,
        ref=ref,
        path=path,
        sha256=sha,
        semver=str(raw.get("semver", "0.0.0")),
        ep=str(raw.get("ep", "")),
        # approved_by/at may be null in an in-flight (unapproved) manifest entry;
        # normalize to "" so ArtifactEntry.approved is a clean bool.
        approved_by="" if raw.get("approved_by") in (None, "") else str(raw["approved_by"]),
        approved_at="" if raw.get("approved_at") in (None, "") else str(raw["approved_at"]),
        diff_url=str(raw.get("diff_url", "") or ""),
    )


def parse_manifest(raw: dict[str, Any]) -> Manifest:
    if not isinstance(raw, dict):
        raise ManifestError("manifest root must be a mapping")
    if "base" not in raw or "prompts" not in raw:
        raise ManifestError("manifest must define 'base' and 'prompts'")
    prompts_raw = raw["prompts"]
    if not isinstance(prompts_raw, dict) or not prompts_raw:
        raise ManifestError("manifest 'prompts' must be a non-empty mapping")
    prompts = {aid: _entry(KIND_PROMPT, aid, e) for aid, e in prompts_raw.items()}
    routing_raw = raw.get("routing")
    routing = _entry(KIND_ROUTING, "routing", routing_raw) if routing_raw else None
    if routing is None:
        raise ManifestError("manifest must define 'routing'")
    return Manifest(
        version=int(raw.get("version", 1)),
        base=_entry(KIND_PROMPT_BASE, "base", raw["base"]),
        routing=routing,
        prompts=prompts,
    )


def load_manifest(repo_root: Path, manifest_path: Path | None = None) -> Manifest:
    path = manifest_path or (repo_root / MANIFEST_REL)
    if not path.exists():
        raise ManifestError(f"evolution manifest not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_manifest(raw)


__all__ = [
    "KIND_PROMPT",
    "KIND_PROMPT_BASE",
    "KIND_ROUTING",
    "MANIFEST_REL",
    "PROMPTS_REL",
    "ArtifactEntry",
    "Manifest",
    "ManifestError",
    "content_sha256",
    "load_manifest",
    "parse_manifest",
    "split_front_matter",
]
