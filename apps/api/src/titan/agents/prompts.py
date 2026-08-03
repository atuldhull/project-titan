"""System-prompt composition engine (Vol 2 §8, TITAN-AGT-021; Vol 10 §6.1).

An agent's system prompt is assembled at call time from its
:class:`~titan.agents.definition.AgentDefinition` and a Jinja2 template, in the
NORMATIVE seven-part order of §8:

  1. identity & mission            2. canonical-stack & platform constraints
  3. authority (decides_alone / must_escalate, RENDERED FROM THE YAML — the
     single source of truth; no template hard-codes authority, closing the R5
     authority-drift risk)
  4. role heuristics & rubrics     5. output JSON schema
  6. retrieved memory (provenance-tagged)
  7. current task / thread context

**Phase 83 (TITAN-EVO-015): the prompt registry is the single source of truth.**
Templates live in the evolution registry at ``evolution/prompts/<agent>/system.md``
(YAML front-matter + a Jinja body that ``{% extends "_base/system.md" %}``). The
composer no longer reads a bare templates dir; it resolves each agent's prompt
through the :class:`~titan.evolution.resolver.EvolutionResolver`, which enforces
the human-approval gate (TITAN-EVO-001) and returns the pinned ``prompt_sha``
logged on every LLM span. There is no second prompt source.

Templates are trusted, in-repo, and versioned (changes go through an EP pull
request), but rendered through a :class:`SandboxedEnvironment` as defence in depth.
"""

from jinja2 import StrictUndefined, TemplateNotFound
from jinja2.sandbox import SandboxedEnvironment

from titan.agents.definition import AgentDefinition
from titan.evolution.resolver import EvolutionError, EvolutionResolver, FrontMatterLoader

# The shared base skeleton, in the registry. Role templates extend it by this name.
BASE_TEMPLATE = "_base/system.md"

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
    """Renders an agent's system prompt from its definition + the approved,
    SHA-pinned registry template (Vol 10 §6.1)."""

    def __init__(self, resolver: EvolutionResolver) -> None:
        self._resolver = resolver
        self._env = SandboxedEnvironment(
            loader=FrontMatterLoader(str(resolver.prompts_dir)),
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
        """Compose ``definition``'s full system prompt from its APPROVED registry
        template. Raises :class:`EvolutionError` (fail-closed) if the agent's
        prompt — or the base it extends — is unapproved or has drifted from the
        approved bytes (TITAN-EVO-001)."""
        resolved = self._resolver.resolve_prompt(definition.agent_id)
        template = self._env.get_template(resolved.template_name)
        rendered = template.render(agent=definition, task_context=task_context, memory=memory)
        return rendered.strip() + "\n"

    def prompt_sha(self, definition: AgentDefinition) -> str:
        """The pinned content hash of the approved prompt serving this agent —
        stamped on the LLM span + usage ledger (TITAN-EVO-015)."""
        return self._resolver.resolve_prompt(definition.agent_id).sha256

    def has_template(self, definition: AgentDefinition) -> bool:
        try:
            resolved = self._resolver.resolve_prompt(definition.agent_id)
            self._env.get_template(resolved.template_name)
        except (TemplateNotFound, EvolutionError):
            return False
        return True


__all__ = ["BASE_TEMPLATE", "SECTION_MARKERS", "PromptComposer"]
