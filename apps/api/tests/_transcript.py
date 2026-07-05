"""The one golden multi-tool, multi-turn transcript (Phase 15/16, TITAN-INT-061).

Shared by the wire-fixture tests and the four-backend conformance matrix so there
is exactly ONE transcript and ONE set of canonical assertions — swapping the
backend must not change the round-tripped canonical result.
"""

import json
from typing import Any

from titan.llm.gateway import LlmGateway
from titan.llm.models import (
    ChatRequest,
    ChatResponse,
    Message,
    StreamDelta,
    TextPart,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
)

from .conftest import run_async

_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}
TOOLS = [
    ToolDef(name="get_weather", description="Weather for a city.", input_schema=_SCHEMA),
    ToolDef(name="get_time", description="Time in a city.", input_schema=_SCHEMA),
]


def assistant_from(resp: ChatResponse) -> Message:
    parts: list[Any] = []
    if resp.text:
        parts.append(TextPart(text=resp.text))
    parts.extend(ToolUsePart(id=c.id, name=c.name, input=c.arguments) for c in resp.tool_calls)
    return Message(role="assistant", content=parts)


def run_golden_transcript(gateway: LlmGateway) -> tuple[ChatResponse, ChatResponse]:
    """Two turns: request → parallel tool_use → tool_results → final text."""

    async def run() -> tuple[ChatResponse, ChatResponse]:
        first = ChatRequest(
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="Weather and time in Paris?"),
            ],
            tools=TOOLS,
            max_tokens=1024,
        )
        r1 = await gateway.chat(first)
        convo = list(first.messages)
        convo.append(assistant_from(r1))
        for call in r1.tool_calls:
            convo.append(
                Message(role="tool", content=[ToolResultPart(tool_use_id=call.id, content="ok")])
            )
        r2 = await gateway.chat(ChatRequest(messages=convo, tools=TOOLS, max_tokens=1024))
        return r1, r2

    result: tuple[ChatResponse, ChatResponse] = run_async(run)
    return result


def assert_golden_canonical(
    r1: ChatResponse, r2: ChatResponse, *, provider: str, model_id: str
) -> None:
    """The provider-invariant canonical transcript every backend must produce."""
    assert r1.provider == provider
    assert r1.model_id == model_id
    assert r1.finish_reason == "tool_use"
    assert {c.name for c in r1.tool_calls} == {"get_weather", "get_time"}
    assert all(c.arguments == {"city": "Paris"} for c in r1.tool_calls)
    assert r1.usage.input_tokens > 0
    assert r2.finish_reason == "stop"
    assert r2.text is not None and "Paris" in r2.text


# --- streaming golden (single-turn: text + one tool call) ------------------


def stream_request() -> ChatRequest:
    return ChatRequest(
        messages=[Message(role="user", content="Weather in Paris?")],
        tools=[TOOLS[0]],
        max_tokens=256,
        stream=True,
    )


def collect_stream(gateway: LlmGateway) -> list[StreamDelta]:
    async def run() -> list[StreamDelta]:
        return [d async for d in gateway.stream(stream_request())]

    result: list[StreamDelta] = run_async(run)
    return result


def assert_golden_stream(deltas: list[StreamDelta]) -> None:
    text = "".join(d.text or "" for d in deltas if d.type == "text")
    assert text == "Checking."
    starts = [d for d in deltas if d.type == "tool_use_start"]
    assert len(starts) == 1 and starts[0].tool_name == "get_weather"
    args = "".join(d.arguments_delta or "" for d in deltas if d.type == "tool_use_delta")
    assert json.loads(args) == {"city": "Paris"}  # parsed: providers differ on whitespace
    done = deltas[-1]
    assert done.type == "done" and done.finish_reason == "tool_use"
    assert done.usage is not None and done.usage.output_tokens == 12
