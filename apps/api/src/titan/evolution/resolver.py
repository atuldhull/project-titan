"""The evolution resolver — the human-approval gate over the prompt registry
(Vol 10 §2, TITAN-EVO-001/002/015).

The prompt composer consults this BEFORE rendering any agent's system prompt. It
enforces, from the git-versioned manifest alone (no DB round-trip on the hot
path):

  * the EP gate — refuse any prompt whose manifest entry lacks ``approved_by`` /
    ``approved_at`` (TITAN-EVO-001a: "the orchestrator refuses to load any
    evolution artifact whose EP lacks an approved_by user id and signed
    timestamp");
  * content pinning — refuse any prompt whose on-disk bytes do not hash to the
    approved ``sha256`` (TITAN-EVO-002: an unapproved edit to the working tree is
    never served; a ``git revert`` back to an approved version is);
  * the base skeleton is part of the rendered prompt, so it is gated too.

``resolve_prompt(agent_id).sha256`` is the *prompt SHA* stamped on every LLM span
and usage-ledger row (TITAN-EVO-015). Why the manifest and not the DB is the
runtime gate: the orchestrator cannot phone GitHub to check a merge, so the
enforceable, reversible source of truth is the in-repo manifest whose approval
fields a human sets in the merged EP PR (ADR-EVO-01). The ``evolution_artifacts``
DB table is the immutable, queryable ledger + audit record of those approvals
(TITAN-EVO-002), synced from this manifest — never a second prompt source.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from titan.evolution.manifest import (
    ArtifactEntry,
    Manifest,
    content_sha256,
    load_manifest,
    split_front_matter,
)


class EvolutionError(RuntimeError):
    """A prompt was refused because it is unapproved or has drifted from the
    approved bytes. Fails the turn CLOSED — an unapproved prompt is never served."""


@dataclass(frozen=True)
class ResolvedPrompt:
    agent_id: str
    template_name: str  # relative to the prompts dir, e.g. 'reviewer/system.md'
    sha256: str
    semver: str
    ep: str


class FrontMatterLoader(FileSystemLoader):
    """A Jinja loader that strips a prompt file's YAML front-matter before the
    template body is compiled, so both role templates and ``_base/system.md``
    render exactly as authored while carrying declarative metadata on disk."""

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        source, filename, uptodate = super().get_source(environment, template)
        _, body = split_front_matter(source)
        return body, filename, uptodate


class EvolutionResolver:
    """Resolves + gates agent prompts against the pinned manifest."""

    def __init__(self, repo_root: Path, prompts_dir: Path, manifest: Manifest) -> None:
        self._root = repo_root
        self._prompts_dir = prompts_dir
        self._manifest = manifest

    @classmethod
    def load(
        cls,
        prompts_dir: Path,
        *,
        repo_root: Path | None = None,
        manifest_path: Path | None = None,
    ) -> "EvolutionResolver":
        # ``evolution/prompts`` sits two levels under the repo root; the container
        # may pass an absolute prompts_dir, so derive the root from it unless the
        # caller pins one (tests point at a temp tree).
        root = repo_root if repo_root is not None else prompts_dir.parents[1]
        return cls(root, prompts_dir, load_manifest(root, manifest_path))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    @property
    def repo_root(self) -> Path:
        return self._root

    @property
    def prompts_dir(self) -> Path:
        return self._prompts_dir

    def reload(self) -> None:
        """Re-read the manifest from disk (after a deploy that landed a new EP)."""
        self._manifest = load_manifest(self._root)

    def _verify(self, entry: ArtifactEntry) -> None:
        if not entry.approved:
            raise EvolutionError(
                f"evolution artifact '{entry.ref}' is not approved "
                f"(EP {entry.ep or '?'} lacks approved_by/approved_at) — refusing to load "
                f"(TITAN-EVO-001)"
            )
        path = self._root / entry.path
        if not path.exists():
            raise EvolutionError(
                f"evolution artifact '{entry.ref}' is missing on disk: {entry.path}"
            )
        disk = content_sha256(path.read_bytes())
        if disk != entry.sha256:
            raise EvolutionError(
                f"evolution artifact '{entry.ref}' on-disk hash {disk[:12]} != approved "
                f"{entry.sha256[:12]} — unapproved change, refusing to load (TITAN-EVO-002)"
            )

    def resolve_prompt(self, agent_id: str) -> ResolvedPrompt:
        """The approved prompt for ``agent_id``. Raises :class:`EvolutionError`
        (fail-closed) if the prompt — or the base it extends — is unapproved or
        has drifted from the approved bytes.

        Re-verifies the on-disk bytes on EVERY call (no result cache): a
        content-pinning guarantee that only held on the first resolve per process
        would let a later working-tree edit be rendered under a stale approved SHA
        (the Jinja loader re-reads the file each turn). The registry is a small,
        read-only mount, so per-turn re-hashing is cheap and the verify→render
        window is closed in practice by mounting it read-only."""
        entry = self._manifest.prompts.get(agent_id)
        if entry is None:
            raise EvolutionError(f"no evolution manifest entry for agent '{agent_id}'")
        # Gate the role prompt AND the shared base it {% extends %} — the rendered
        # system prompt is the composition of both.
        self._verify(entry)
        self._verify(self._manifest.base)
        return ResolvedPrompt(
            agent_id=agent_id,
            template_name=entry.template_name(),
            sha256=entry.sha256,
            semver=entry.semver,
            ep=entry.ep,
        )

    def prompt_sha(self, agent_id: str) -> str:
        """The approved content hash to stamp on the span/ledger for a turn."""
        return self.resolve_prompt(agent_id).sha256

    def resolve_standalone(self, ref: str) -> tuple[str, str]:
        """Verify + return ``(body_text, sha256)`` for a STANDALONE registry prompt
        (no Jinja render, no base gating) — e.g. the CEO intake prompts, which are
        static text carrying literal JSON braces that must not be interpreted.

        Same fail-closed gate as :meth:`resolve_prompt` (unapproved / drifted /
        missing → :class:`EvolutionError`), so the intake path is a first-class,
        pinned registry consumer rather than a second, ungated prompt source."""
        entry = self._manifest.prompts.get(ref)
        if entry is None:
            raise EvolutionError(f"no evolution manifest entry for prompt '{ref}'")
        self._verify(entry)
        raw = (self._root / entry.path).read_text(encoding="utf-8")
        _, body = split_front_matter(raw)
        return body.strip(), entry.sha256

    def active_prompt_versions(self) -> dict[str, dict[str, str]]:
        """agent_id -> {sha256, semver, ep} for the dashboard (TITAN-EVO-015c).

        Reports the manifest-declared version for every agent WITHOUT gating, so
        the surface can show an unapproved/in-flight entry too; the runtime path
        (:meth:`resolve_prompt`) still refuses to serve it."""
        return {
            aid: {"sha256": e.sha256, "semver": e.semver, "ep": e.ep, "approved": str(e.approved)}
            for aid, e in self._manifest.prompts.items()
        }


__all__ = ["EvolutionError", "EvolutionResolver", "FrontMatterLoader", "ResolvedPrompt"]
