"""``task.*`` builtin tools (provider ``builtin:tasks``) — the "task CRUD"
class of the Vol 8 §4.3 allowlists, bridged to the orchestrator's public
service interface (no second task API).

Phase 18 ships the read half (``task.get``); richer task tools arrive with the
agent runtime. Missing and out-of-project tasks are indistinguishable in the
error (TITAN-SEC-032: existence is never leaked to an out-of-scope caller).
"""

import uuid
from typing import Any

from titan.gateway.problems import ProblemException
from titan.orchestrator import service as orchestrator
from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec

_GET_INPUT: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"task_id": {"type": "string", "format": "uuid"}},
    "required": ["task_id"],
    "additionalProperties": False,
}

_GET_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "project_id": {"type": "string"},
        "title": {"type": "string"},
        "spec": {"type": "string"},
        "status": {"type": "string"},
        "priority": {"type": "integer"},
        "assignee_agent_id": {"type": ["string", "null"]},
    },
    "required": ["id", "project_id", "title", "status"],
}


async def _get_task(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    task_id = uuid.UUID(args["task_id"])
    async with ctx.engine.connect() as conn:
        try:
            task = await orchestrator.get_task(conn, task_id)
        except ProblemException as exc:
            raise ToolError("PERMISSION_DENIED", "no such task in this project") from exc
    if task.project_id != ctx.project_id:
        raise ToolError("PERMISSION_DENIED", "no such task in this project")
    return ExecutionOutcome(
        result={
            "id": str(task.id),
            "project_id": str(task.project_id),
            "title": task.title,
            "spec": task.spec,
            "status": task.status,
            "priority": task.priority,
            "assignee_agent_id": (
                str(task.assignee_agent_id) if task.assignee_agent_id is not None else None
            ),
        }
    )


SPECS = (
    ToolSpec(
        name="task.get",
        provider="builtin:tasks",
        description="Fetch one task (title, spec, status, priority, assignee) by id.",
        risk="read",
        required_scopes=("task:read",),
        input_schema=_GET_INPUT,
        output_schema=_GET_OUTPUT,
        executor=_get_task,
        timeout_s=10.0,
        rate_limit_per_min=120,
    ),
)

__all__ = ["SPECS"]
