"""``message.post`` builtin tool (provider ``builtin:messaging``) — the
"messaging" class of the Vol 8 §4.3 allowlists, bridged to the messaging
module's public service interface.

The sender is ALWAYS the bus-authenticated agent (``ctx.agent_uuid``), never
tool arguments — attribution cannot be spoofed by the model.
"""

import json
import uuid
from typing import Any

from titan.gateway.problems import ProblemException
from titan.messaging import service as messaging
from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec

_POST_INPUT: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(messaging.MESSAGE_KINDS)},
        "body": {"type": "object"},
        "thread_id": {"type": "string", "format": "uuid"},
        "task_id": {"type": "string", "format": "uuid"},
    },
    "required": ["kind", "body"],
    "additionalProperties": False,
}

_MAX_BODY_BYTES = 64 * 1024


def _body_size_rule(args: dict[str, Any]) -> "tuple[Any, str] | None":
    # TITAN-SEC-070 length bounds: the body is free-form JSON, so the cap is a
    # parameter constraint (JSON Schema can't bound serialized size). Measure
    # true UTF-8 bytes (what jsonb stores), not ASCII-escaped chars, so Unicode
    # bodies under the cap aren't wrongly rejected.
    serialized = json.dumps(args.get("body", {}), default=str, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > _MAX_BODY_BYTES:
        return ("SCHEMA_INVALID", f"at '/body': serialized body exceeds {_MAX_BODY_BYTES} bytes")
    return None


_POST_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string"},
        "thread_id": {"type": "string"},
    },
    "required": ["message_id", "thread_id"],
}


async def _post_message(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    if ctx.agent_uuid is None:
        raise ToolError("PERMISSION_DENIED", "caller has no registered agent identity to post as")
    task_id = uuid.UUID(args["task_id"]) if "task_id" in args else ctx.task_id
    thread_id = uuid.UUID(args["thread_id"]) if "thread_id" in args else None
    data = messaging.MessageCreate(
        kind=args["kind"], body=args["body"], thread_id=thread_id, task_id=task_id
    )
    async with ctx.engine.begin() as conn:
        try:
            message = await messaging.create_message(
                conn,
                ctx.project_id,
                data,
                sender_agent_id=ctx.agent_uuid,
                sender_user_id=None,
                actor=ctx.agent_id,
            )
        except ProblemException as exc:
            # Kind-specific body validation failures surface with the
            # messaging module's detail so the agent can self-correct.
            raise ToolError("UPSTREAM_ERROR", f"message rejected: {exc.detail}") from exc
    return ExecutionOutcome(
        result={"message_id": str(message.id), "thread_id": str(message.thread_id)}
    )


SPECS = (
    ToolSpec(
        name="message.post",
        provider="builtin:messaging",
        description=(
            "Post a message to the project's communication bus "
            "(progress reports, reviews, bug reports, decisions)."
        ),
        risk="write",
        required_scopes=("message:write",),
        input_schema=_POST_INPUT,
        output_schema=_POST_OUTPUT,
        executor=_post_message,
        timeout_s=10.0,
        rate_limit_per_min=30,
        constraints=(_body_size_rule,),
    ),
)

__all__ = ["SPECS"]
