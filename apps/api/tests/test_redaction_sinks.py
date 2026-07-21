"""TITAN-SEC-054 DoD: one planted secret of EACH class (live-inventory value,
high-entropy token, provider-pattern key) appears redacted at every sink —

  (a) the captured gateway request payload (the exact bytes a provider would
      receive, recorded off the httpx wire by a MockTransport),
  (b) every log line before Loki ingestion (structlog-native AND foreign
      stdlib records, parsed off real stdout),
  (c) the sandbox output stream (tool.output pub/sub frames off fakeredis),
      including a token SPLIT across two docker frames.

No DB needed: the inventory is loaded onto the process-global engine exactly
the way the vault refresh does it (InventoryIndex.from_values → set_inventory);
the autouse conftest fixture resets the engine afterwards.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import structlog

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import (
    ChatRequest,
    EmbeddingsRequest,
    Message,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from titan.observability.logging import configure_logging
from titan.redaction import Hit, InventoryIndex, RedactionReporter, default_engine
from titan.tools.streaming import ToolOutputStream, channel_for

from .conftest import run_async

# One secret per SEC-054 class.
INVENTORY_SECRET = "vault-value-hunter2-9000"
ENTROPY_SECRET = "9fXk2LmQ7pZ4tRv8WqYs1JhN5B3d"
PROVIDER_SECRET = "sk-ant-api03-PlantedKey1234567890"
ALL_SECRETS = (INVENTORY_SECRET, ENTROPY_SECRET, PROVIDER_SECRET)


@pytest.fixture(autouse=True)
def _live_inventory() -> None:
    """Load the planted inventory value the way the vault refresh does."""
    default_engine().set_inventory(InventoryIndex.from_values([INVENTORY_SECRET]))


def _assert_clean(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, f"planted secret leaked: {secret[:8]}…"
    assert "[REDACTED:" in text


# --- sink (a): the gateway payload a provider would receive -------------------

_CONFIG = """
providers:
  anthropic: { api_key: test-key, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  chat-default:  { provider: anthropic, model: claude-sonnet-4-5 }
  embed-default: { provider: ollama, model: nomic-embed-text, dimensions: 768 }
routing:
  default: chat-default
  embeddings_default: embed-default
"""


def _handler(records: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        records.append(body)
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            )
        if request.url.path.endswith("/v1/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1] * 768}],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )
        return httpx.Response(404, json={"error": "unhandled"})

    return handler


def _gateway(tmp_path: Path, records: list[dict[str, Any]]) -> LlmGateway:
    config = tmp_path / "models.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(records)))
    return LlmGateway(ConfigLoader(config), client)


def test_gateway_chat_payload_is_redacted_for_all_three_classes(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
    request = ChatRequest(
        messages=[
            Message(role="system", content=f"context bundle holds {INVENTORY_SECRET}"),
            Message(
                role="user",
                content=[TextPart(text=f"token {ENTROPY_SECRET} and key {PROVIDER_SECRET}")],
            ),
            Message(
                role="assistant",
                content=[ToolUsePart(id="t1", name="fs.write", input={"content": PROVIDER_SECRET})],
            ),
            Message(
                role="tool",
                content=[ToolResultPart(tool_use_id="t1", content=f"stdout: {INVENTORY_SECRET}")],
            ),
        ],
        max_tokens=64,
    )
    response = run_async(lambda: gateway.chat(request))
    assert response.text == "ok"  # redaction never breaks the call itself
    assert len(records) == 1
    _assert_clean(json.dumps(records[0]))  # the CAPTURED provider wire body


def test_gateway_stream_payload_is_redacted(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)

    async def consume() -> None:
        request = ChatRequest(
            messages=[Message(role="user", content=f"leak {PROVIDER_SECRET}")],
            max_tokens=32,
            stream=True,
        )
        async for _delta in gateway.stream(request):
            pass

    # The mock transport serves a non-SSE body; the adapter may fail parsing
    # the stream, but the REQUEST — the thing under test — has left by then.
    with contextlib.suppress(Exception):
        run_async(consume)
    assert records, "stream request never reached the transport"
    assert PROVIDER_SECRET not in json.dumps(records[0])


def test_gateway_embeddings_input_is_redacted(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
    request = EmbeddingsRequest(input=[f"chunk with {INVENTORY_SECRET}", "clean chunk"])
    response = run_async(lambda: gateway.embed(request))
    assert response.dimensions == 768
    embed_body = json.dumps(records[0])
    assert INVENTORY_SECRET not in embed_body
    assert "clean chunk" in embed_body  # ordinary text sails through


def test_without_inventory_the_vault_value_would_leak(tmp_path: Path) -> None:
    """The negative control that proves the inventory detector is what catches
    the vault value: an engine with no inventory loaded lets it through
    (it is deliberately not pattern- or entropy-shaped)."""
    from titan.redaction import EMPTY_INVENTORY

    default_engine().set_inventory(EMPTY_INVENTORY)
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
    request = ChatRequest(
        messages=[Message(role="user", content=f"x {INVENTORY_SECRET}")], max_tokens=16
    )
    run_async(lambda: gateway.chat(request))
    assert INVENTORY_SECRET in json.dumps(records[0])


# --- sink (b): every log line before Loki --------------------------------------


def _log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_log_lines_redact_all_three_classes(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="titan-test")
    logger = structlog.get_logger("titan.test")
    logger.info(
        "deploy.failed",
        note=f"env leaked {INVENTORY_SECRET}",
        detail={"nested": f"token {ENTROPY_SECRET}"},
        key=PROVIDER_SECRET,
    )
    lines = _log_lines(capsys)
    assert lines, "no JSON log line captured"
    _assert_clean(json.dumps(lines))
    assert lines[0]["event"] == "deploy.failed"  # identity fields survive


def test_foreign_stdlib_records_are_redacted_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="titan-test")
    logging.getLogger("uvicorn.error").error("handshake failed for key %s", PROVIDER_SECRET)
    payload = json.dumps(_log_lines(capsys))
    assert PROVIDER_SECRET not in payload
    assert "[REDACTED:anthropic-key:" in payload


def test_log_redaction_fails_closed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing engine must DROP the line's payload, never emit it raw."""
    import titan.observability.logging as obslog

    configure_logging(service="titan-test")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("engine down")

    monkeypatch.setattr(obslog, "redact_structure", boom)
    structlog.get_logger("titan.test").info("oops", secret_note=PROVIDER_SECRET)
    out = capsys.readouterr().out
    assert PROVIDER_SECRET not in out
    assert "log.redaction_failed" in out


def test_redaction_hits_reach_the_reporter_from_the_log_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = RedactionReporter()
    default_engine().set_reporter(reporter.record)
    configure_logging(service="titan-test")
    structlog.get_logger("titan.test").info("x", note=PROVIDER_SECRET)
    capsys.readouterr()
    records = reporter.drain()
    assert any(r.sink == "log" and r.cls == "anthropic-key" for r in records)


# --- sink (c): sandbox output stream --------------------------------------------


class _Collector:
    """Subscribe to the call's tool.output channel and collect frames. Pub/sub
    is fire-and-forget, so the reader runs CONCURRENTLY with the publisher
    (same shape as the terminal streaming test)."""

    def __init__(self, redis: fakeredis.aioredis.FakeRedis, project_id: uuid.UUID) -> None:
        self._redis = redis
        self._channel = channel_for(project_id)

    async def collect(self, emit: Callable[[], Any]) -> list[dict[str, Any]]:
        sub = self._redis.pubsub()
        await sub.subscribe(self._channel)
        frames: list[dict[str, Any]] = []

        async def reader() -> None:
            async for message in sub.listen():
                if message["type"] == "message":
                    frames.append(json.loads(message["data"]))

        reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0)  # let the subscriber start listening
        try:
            await emit()
            await asyncio.sleep(0.1)  # let the tail drain
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
            await sub.aclose()  # type: ignore[no-untyped-call]
        return frames


def _stream(redis: fakeredis.aioredis.FakeRedis, project_id: uuid.UUID) -> ToolOutputStream:
    return ToolOutputStream(
        redis,
        project_id=project_id,
        task_id=uuid.uuid4(),
        agent_id="agent.backend.1",
        tool="terminal.run",
        tool_call_id="call-1",
        redactor=lambda text: str(default_engine().redact(text, sink="sandbox-stream")),
    )


def test_stream_chunks_redact_all_three_classes() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    project_id = uuid.uuid4()
    stream = _stream(redis, project_id)
    collector = _Collector(redis, project_id)

    async def emit() -> None:
        await stream.emit("stdout", f"env={INVENTORY_SECRET}\n")
        await stream.emit("stdout", f"key={PROVIDER_SECRET}\ntoken={ENTROPY_SECRET}")
        await stream.flush()

    frames = run_async(lambda: collector.collect(emit))
    assert frames, "no tool.output frames received"
    reassembled = "".join(f["chunk"] for f in frames)
    _assert_clean(reassembled)
    assert [f["seq"] for f in frames] == list(range(len(frames)))


def test_stream_redacts_a_token_split_across_two_docker_frames() -> None:
    """Docker frames split arbitrarily — the line buffer must reassemble a
    secret that straddles two emit() calls before redacting."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    project_id = uuid.uuid4()
    stream = _stream(redis, project_id)
    collector = _Collector(redis, project_id)
    half = len(PROVIDER_SECRET) // 2

    async def emit() -> None:
        await stream.emit("stdout", f"key={PROVIDER_SECRET[:half]}")
        await stream.emit("stdout", f"{PROVIDER_SECRET[half:]} done\n")

    frames = run_async(lambda: collector.collect(emit))
    reassembled = "".join(f["chunk"] for f in frames)
    assert PROVIDER_SECRET not in reassembled
    assert "[REDACTED:anthropic-key:" in reassembled
    assert reassembled.endswith("done\n")


def test_stream_fails_closed_when_the_redactor_crashes() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    project_id = uuid.uuid4()

    def boom(text: str) -> str:
        raise RuntimeError("redactor down")

    stream = ToolOutputStream(
        redis,
        project_id=project_id,
        task_id=None,
        agent_id="agent.backend.1",
        tool="terminal.run",
        tool_call_id="call-1",
        redactor=boom,
    )
    collector = _Collector(redis, project_id)

    async def emit() -> None:
        await stream.emit("stdout", f"secret {PROVIDER_SECRET}\n")

    frames = run_async(lambda: collector.collect(emit))
    payload = json.dumps(frames)
    assert PROVIDER_SECRET not in payload
    assert "[REDACTION-ERROR]" in payload


def test_tool_envelope_redactor_covers_the_inventory_class() -> None:
    """The bus envelope path (result/stderr the LLM sees) rides
    default_redactor, which delegates to the same engine — the vault value is
    caught there too, not just token shapes."""
    from titan.tools.redaction import default_redactor

    out = default_redactor({"stdout": f"cat .env → {INVENTORY_SECRET}"})
    assert INVENTORY_SECRET not in json.dumps(out)


@pytest.mark.schema
def test_bus_wires_its_redactor_into_the_output_stream(
    migrated_engine: Any, tmp_path: Path
) -> None:
    """The wiring proof (the Phase-67 lesson: building the redactor is not
    wiring it into the ONE path that needs it): a tool emitting through the
    BUS-constructed stream — not a hand-built one — publishes redacted chunks,
    because bus.py hands its own redactor to ToolOutputStream."""
    from titan.tools.bus import ToolBus, ToolCaller
    from titan.tools.envelopes import new_call
    from titan.tools.registry import ExecutionOutcome, ToolRegistry, ToolSpec
    from titan.tools.sandbox.controller import SandboxController
    from titan.tools.sandbox.policy import SandboxPolicy
    from titan.tools.sandbox.session import SandboxSessionManager
    from titan.tools.sandbox.workspace import WorkspaceManager

    registry = ToolRegistry()

    async def spew(_args: dict[str, Any], ctx: Any) -> ExecutionOutcome:
        assert ctx.sandbox is not None
        await ctx.sandbox.stream.emit("stdout", f"key {PROVIDER_SECRET}\nno newline tail")
        return ExecutionOutcome(result={})

    registry.register(
        ToolSpec(
            name="test.spew",
            provider="builtin:test",
            description="emits through the bus-built stream",
            risk="read",
            required_scopes=("test:run",),
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            executor=spew,
            timeout_s=5.0,
        )
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = ToolBus(
        registry,
        migrated_engine,
        redis=redis,
        workspace_provider=WorkspaceManager(tmp_path / "ws"),
        # The session handle is lazy — no container is created unless a tool
        # calls sandbox.get(), so no Docker is needed to reach stream.emit.
        sandbox_manager=SandboxSessionManager(SandboxController(SandboxPolicy(image="unused"))),
    )
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=uuid.uuid4(),
        granted_scopes=frozenset({"test:run"}),
        task_id=uuid.uuid4(),
        initiator_chain=("user:founder",),
    )
    collector = _Collector(redis, caller.project_id)

    async def emit() -> None:
        envelope = await bus.invoke(
            new_call(
                "test.spew",
                {},
                project_id=caller.project_id,
                task_id=caller.task_id,
                agent_id=caller.agent_id,
            ),
            caller,
        )
        assert envelope.status == "ok", envelope.message

    frames = run_async(lambda: collector.collect(emit))
    assert frames, "the bus-built stream published nothing"
    reassembled = "".join(f["chunk"] for f in frames)
    assert PROVIDER_SECRET not in reassembled
    assert "[REDACTED:anthropic-key:" in reassembled
    # The bus flushed the held partial line when the executor finished.
    assert "no newline tail" in reassembled


def test_redaction_hash8_correlates_across_sinks(capsys: pytest.CaptureFixture[str]) -> None:
    """The same secret produces the SAME hash8 marker at every sink — the
    audit-trail correlation SEC-054's replacement format exists for."""
    from titan.tools.redaction import default_redactor

    hits = default_engine().redact_with_hits(PROVIDER_SECRET)[1]
    assert hits == [Hit(cls="anthropic-key", hash8=hits[0].hash8)]
    marker = f"[REDACTED:anthropic-key:{hits[0].hash8}]"
    configure_logging(service="titan-test")
    structlog.get_logger("titan.test").info("x", k=PROVIDER_SECRET)
    assert marker in capsys.readouterr().out  # log sink
    assert marker in str(default_redactor(f"k={PROVIDER_SECRET}"))  # envelope sink
