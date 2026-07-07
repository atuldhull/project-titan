"""System-prompt composition engine (Vol 2 §8, TITAN-AGT-021).

An agent's system prompt is assembled at call time from its
:class:`~titan.agents.definition.AgentDefinition` and a Jinja2 template, in the
NORMATIVE seven-part order of §8:

  1. identity & mission            2. canonical-stack & platform constraints
  3. authority (decides_alone / must_escalate, RENDERED FROM THE YAML — the
     single source of truth; no template hard-codes authority, closing the R5
     authority-drift risk)
  4. role heuristics & rubrics     5. output JSON schema
  6. retrieved memory (provenance-tagged; a placeholder until the memory phases)
  7. current task / thread context

Every ``agents/<agent_id>.j2`` template ``{% extends "agents/_base.j2" %}`` and
fills only the role-specific blocks (mission, constraints delta, heuristics,
output schema). The base owns the part order and the authority render, so the
sequence is identical for all sixteen roles and can never drift per template.

Templates are trusted, in-repo, and versioned (prompt changes go through PR
review), but rendered through a :class:`SandboxedEnvironment` as defence in depth
— a template can never reach out of its sandbox even if one day it interpolates
model-derived text.
"""

from functools import lru_cache
from pathlib import Path

from jinja2 import FileSystemLoader, StrictUndefined, TemplateNotFound
from jinja2.sandbox import SandboxedEnvironment

from titan.agents.definition import AgentDefinition
from titan.agents.loader import resolve_config_dir

BASE_TEMPLATE = "agents/_base.j2"

# The seven normative section markers the base renders, in order (§8). Kept here
# so the composer, the snapshot test, and readers share one source of truth.
SECTION_MARKERS: tuple[str, ...] = (
    "# 1. Identity & Mission",
    "# 2. Platform Constraints",
    "# 3. Authority",
    "# 4. Heuristics & Rubrics",
    "# 5. Output Schema",
    "# 6. Retrieved Memory",
    "# 7. Task Context",
)


class PromptComposer:
    """Renders an agent's system prompt from its definition + a Jinja2 template."""

    def __init__(self, prompts_dir: Path) -> None:
        self._dir = prompts_dir
        self._env = SandboxedEnvironment(
            loader=FileSystemLoader(str(prompts_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
            undefined=StrictUndefined,  # a missing var is a bug, not a silent blank
            autoescape=False,  # prompts are plain text, never HTML
        )

    def compose(
        self,
        definition: AgentDefinition,
        *,
        task_context: str = "",
        memory: str = "",
    ) -> str:
        """Compose ``definition``'s full system prompt. ``memory`` and
        ``task_context`` are the per-turn parts 6 and 7 (empty until the memory /
        runtime phases supply them)."""
        template = self._env.get_template(definition.system_prompt_template)
        rendered = template.render(agent=definition, task_context=task_context, memory=memory)
        return rendered.strip() + "\n"

    def has_template(self, definition: AgentDefinition) -> bool:
        try:
            self._env.get_template(definition.system_prompt_template)
        except TemplateNotFound:
            return False
        return True


@lru_cache(maxsize=4)
def _composer_for(resolved_dir: str) -> PromptComposer:
    return PromptComposer(Path(resolved_dir))


def build_composer(prompts_dir: str | Path) -> PromptComposer:
    """Build (process-cached) a composer rooted at the prompts dir. The dir is
    resolved cwd-robustly, bounded at the repo root — same rule as the roster."""
    return _composer_for(str(resolve_config_dir(prompts_dir)))


__all__ = ["BASE_TEMPLATE", "SECTION_MARKERS", "PromptComposer", "build_composer"]
