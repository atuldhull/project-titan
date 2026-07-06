"""The Tool Registry (Vol 7 §4.1, TITAN-INT-020).

An in-memory index, rebuilt on boot, mapping namespaced tool names
(``fs.read``, ``git.commit``, ``mcp.<server>.<tool>``) to: JSON-Schema
(draft 2020-12) input AND output schemas, provider id, risk class, required
RBAC scopes, timeout, and rate limits — the §4.1 ``git.commit`` registry-entry
shape. Today the rebuild sources are the builtin provider modules; plugin and
MCP providers (PostgreSQL-backed) join the same rebuild in their phases.

Per turn, an agent's tool list is (role's allowed tools) ∩ (enabled providers)
∩ (project-level policy), emitted in the Phase-15 gateway's canonical
tool-definition format (:class:`~titan.llm.service.ToolDef`) — no parallel
schema dialect.
"""

import fnmatch
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema.validators import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.service import ToolDef
from titan.tools.allowlists import allowed_for_role
from titan.tools.errors import ToolErrorCode
from titan.tools.sandbox.session import SandboxSession
from titan.tools.sandbox.workspace import Workspace

# Namespaced tool names: fs.read, git.commit, mcp.github.search_issues.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_-]*)+$")

RiskClass = str  # 'read' | 'write' | 'admin' (validated at registration)
_RISK_CLASSES = frozenset({"read", "write", "admin"})


@dataclass(frozen=True)
class ToolExecutionContext:
    """What an executor may know about the call: DB access plus attribution.
    Deliberately NO scopes/role here — authorization is the bus's job and is
    finished before an executor ever runs. ``workspace`` is the task's jailed
    ``/workspace`` (None when no workspace provider is wired — fs tools then
    fail closed with TOOL_UNAVAILABLE)."""

    engine: AsyncEngine
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    agent_id: str  # audit label, e.g. "agent.backend_engineer.2"
    agent_uuid: uuid.UUID | None  # DB identity when known (FK attribution)
    workspace: Workspace | None = None
    # The task's running sandbox + live output stream (Phase 21). None when no
    # Docker daemon is reachable — terminal/git/code tools then fail closed.
    sandbox: SandboxSession | None = None


@dataclass
class ExecutionOutcome:
    """What an executor hands back for step 5's capture: the structured result
    plus captured stderr and produced artifacts (§4.2 result envelope halves)."""

    result: dict[str, Any]
    stderr_tail: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)


ToolExecutor = Callable[[dict[str, Any], ToolExecutionContext], Awaitable[ExecutionOutcome]]

# A TITAN-SEC-031 per-parameter rule: pure function over the (already
# schema-valid) arguments; returns (code, message) to reject, None to pass.
# Example: git writes only to titan/task-* branches.
ParamConstraint = Callable[[dict[str, Any]], "tuple[ToolErrorCode, str] | None"]


class ProjectToolPolicy(BaseModel):
    """Project-level tool policy (§4.1 third intersection term). ``None`` for
    ``enabled_providers`` means every provider is enabled."""

    model_config = ConfigDict(extra="forbid")

    disabled_tools: list[str] = Field(default_factory=list)  # fnmatch patterns
    enabled_providers: list[str] | None = None

    def provider_enabled(self, provider: str) -> bool:
        return self.enabled_providers is None or provider in self.enabled_providers

    def tool_enabled(self, name: str) -> bool:
        return not any(fnmatch.fnmatchcase(name, pattern) for pattern in self.disabled_tools)


@dataclass(frozen=True)
class ToolSpec:
    """One registry entry — the §4.1 ``git.commit`` example, plus the executor
    and TITAN-SEC-031 parameter constraints the bus enforces."""

    name: str
    provider: str  # builtin:<module> | plugin:<id> | mcp:<server>
    description: str
    risk: RiskClass
    required_scopes: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    executor: ToolExecutor
    timeout_s: float = 60.0  # §4.3 default per-call wall clock
    rate_limit_per_min: int | None = None  # stored per §4.1; enforced Phase 19
    constraints: tuple[ParamConstraint, ...] = ()

    def tool_def(self) -> ToolDef:
        """This entry in the gateway's canonical tool-definition format."""
        return ToolDef(name=self.name, description=self.description, input_schema=self.input_schema)


class ToolRegistry:
    """The in-memory index. Registration validates the entry hard — a bad tool
    definition is a boot failure, never a runtime surprise."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not _NAME_RE.match(spec.name):
            raise ValueError(f"tool name {spec.name!r} is not a namespaced lowercase identifier")
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        if spec.risk not in _RISK_CLASSES:
            raise ValueError(f"tool {spec.name!r} risk must be one of {sorted(_RISK_CLASSES)}")
        if spec.timeout_s <= 0:
            raise ValueError(f"tool {spec.name!r} timeout_s must be positive")
        for label, schema in (("input", spec.input_schema), ("output", spec.output_schema)):
            Draft202012Validator.check_schema(schema)
            if schema.get("type") != "object":
                raise ValueError(f"tool {spec.name!r} {label}_schema must declare type 'object'")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def tools_for_turn(self, role: str, policy: ProjectToolPolicy) -> list[ToolDef]:
        """(agent role's allowed tools) ∩ (enabled providers) ∩ (project policy),
        in the gateway format the LLM consumes (§4.1). Unknown roles get an
        empty list — fail closed."""
        return [
            spec.tool_def()
            for spec in self.specs()
            if allowed_for_role(role, spec.name)
            and policy.provider_enabled(spec.provider)
            and policy.tool_enabled(spec.name)
        ]


__all__ = [
    "ExecutionOutcome",
    "ParamConstraint",
    "ProjectToolPolicy",
    "RiskClass",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolRegistry",
    "ToolSpec",
]
