"""Phase 25 (walking skeleton) tests: the story parser, the direct WS publisher,
the gateway's metered streaming (usage lands in llm_usage), and the CEO intake
handler end to end against a fake gateway. Hermetic parts run over fakeredis in a
single loop; the persistence/metering parts are ``schema``-marked (Postgres)."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.intake.service import run_intake
from titan.intake.stories import MAX_STORIES, parse_stories
from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message, StreamDelta, Usage
from titan.realtime import channels
from titan.realtime.publisher import RealtimePublisher

from .conftest import run_async


def _fake() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# --- story parser ----------------------------------------------------------


def test_parse_stories_from_json_with_prose_and_fence() -> None:
    text_out = (
        "Sure! Here is the backlog:\n```json\n"
        '[{"title": "As a user, I want to add tasks, so that I track work", '
        '"acceptance_criteria": ["can create a task", "task persists"]},'
        '{"title": "As a user, I want to mark done", "acceptance": "checkbox toggles"}]'
        "\n```\nHope that helps."
    )
    stories = parse_stories(text_out)
    assert [s.title for s in stories] == [
        "As a user, I want to add tasks, so that I track work",
        "As a user, I want to mark done",
    ]
    assert stories[0].acceptance == ("can create a task", "task persists")
    assert "Acceptance criteria:" in stories[0].spec_text()
    assert stories[1].acceptance == ("checkbox toggles",)  # string acceptance normalized


def test_parse_stories_falls_back_to_lines() -> None:
    text_out = "I couldn't format JSON but:\nAs a user, I want search, so that I find tasks.\nOops."
    stories = parse_stories(text_out)
    assert len(stories) == 1
    assert stories[0].title.startswith("As a user, I want search")


def test_parse_stories_empty_and_capped() -> None:
    assert parse_stories("no stories here") == []
    many = json.dumps([{"title": f"As a user, I want feature {i}"} for i in range(30)])
    assert len(parse_stories(many)) == MAX_STORIES


def test_parse_stories_clamps_over_length_title() -> None:
    # A verbose/hostile title is clamped to the task title cap (never > 500) so it
    # can't raise a validation error at persist time (review fix).
    long = "As a user, I want " + "x" * 800 + ", so that Y"
    stories = parse_stories(json.dumps([{"title": long}]))
    assert len(stories) == 1 and len(stories[0].title) == 500


# --- RealtimePublisher -----------------------------------------------------


def test_publisher_delta_is_ephemeral_run_event_is_buffered() -> None:
    async def scenario() -> None:
        redis = _fake()
        pub = RealtimePublisher(redis)
        pid = str(uuid.uuid4())
        ps: Any = redis.pubsub()
        await ps.subscribe(channels.project_channel(pid))
        await ps.get_message(timeout=0.05)

        await pub.publish_delta(pid, "run-1", "hello ", agent_id="agent.ceo")
        await pub.publish_run_event(pid, "run.started", {"run_id": "run-1"})

        got = []
        for _ in range(4):
            message = await ps.get_message(timeout=0.1)
            if message and message["type"] == "message":
                got.append(json.loads(message["data"]))
        by_type = {f["type"]: f for f in got}
        assert by_type["run.output.delta"]["payload"] == {
            "run_id": "run-1",
            "delta": "hello ",
            "agent_id": "agent.ceo",
        }
        # The delta is ephemeral (not buffered); the run event is buffered.
        assert await redis.llen(channels.buffer_key(pid)) == 1
        await ps.aclose()
        await redis.aclose()

    run_async(scenario)


# --- gateway metered streaming --------------------------------------------

_CONFIG = """
providers:
  ollama: { base_url: http://ollama-test:11434 }
model_aliases:
  local-default: { provider: ollama, model: qwen3-coder:30b }
routing:
  default: local-default
"""


def _ollama_stream_handler(chunks: list[str], usage: dict[str, int]) -> "Any":
    def handler(request: httpx.Request) -> httpx.Response:
        lines = [json.dumps({"choices": [{"delta": {"content": c}}]}) for c in chunks]
        lines.append(
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})
        )
        lines.append("[DONE]")
        body = "".join(f"data: {line}\n\n" for line in lines).encode()
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return handler


@pytest.mark.schema
def test_gateway_stream_meters_usage(migrated_engine: AsyncEngine, tmp_path: Path) -> None:
    async def scenario() -> None:
        config = tmp_path / "models.yaml"
        config.write_text(_CONFIG, encoding="utf-8")
        handler = _ollama_stream_handler(
            ["Hello ", "world"], {"prompt_tokens": 30, "completion_tokens": 15}
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = LlmGateway(ConfigLoader(config), client, engine=migrated_engine)
        run_id = uuid7()
        req = ChatRequest(
            model_alias="local-default",
            messages=[Message(role="user", content="hi")],
            max_tokens=64,
            stream=True,
        )
        deltas = [d async for d in gateway.stream(req, run_id=run_id)]
        assert "".join(d.text or "" for d in deltas if d.type == "text") == "Hello world"

        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT input_tokens, output_tokens, provider, model, run_id "
                        "FROM llm_usage WHERE run_id = :rid"
                    ),
                    {"rid": run_id},
                )
            ).one()
        assert row.input_tokens == 30 and row.output_tokens == 15
        assert row.provider == "ollama" and row.run_id == run_id
        await client.aclose()

    run_async(scenario)


# --- run_intake end to end (fake gateway) ----------------------------------


class _FakeGateway:
    """A gateway double whose ``stream`` yields a scripted delta sequence."""

    def __init__(self, script: list[StreamDelta], *, raises: bool = False) -> None:
        self._script = script
        self._raises = raises

    async def stream(
        self, req: ChatRequest, *, run_id: uuid.UUID | None = None
    ) -> AsyncIterator[StreamDelta]:
        for delta in self._script:
            yield delta
        if self._raises:
            raise RuntimeError("provider exploded")


async def _seed_project(engine: AsyncEngine, project_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        uid = uuid7()
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:i, :e, 'CEO', 'local', :s)"
            ),
            {"i": uid, "e": f"ceo-{uid}@titan.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:p, :n, 'Live', 'brief', :u)"
            ),
            {"p": project_id, "n": f"proj-{project_id.hex}", "u": uid},
        )


_STORY_JSON = (
    '[{"title": "As a user, I want to add tasks, so that I track work", '
    '"acceptance_criteria": ["task persists"]}, '
    '{"title": "As a user, I want to filter by status, so that I focus"}]'
)


@pytest.mark.schema
def test_run_intake_streams_and_persists(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        ps: Any = redis.pubsub()
        await ps.subscribe(channels.project_channel(str(pid)))
        await ps.get_message(timeout=0.05)

        script = [
            StreamDelta(type="text", text=_STORY_JSON[:40]),
            StreamDelta(type="text", text=_STORY_JSON[40:]),
            StreamDelta(
                type="done", finish_reason="stop", usage=Usage(input_tokens=12, output_tokens=30)
            ),
        ]
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, _FakeGateway(script)),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="Build a task tracker",
            model_alias="local-default",
        )
        assert count == 2

        frames = []
        for _ in range(10):
            message = await ps.get_message(timeout=0.1)
            if message and message["type"] == "message":
                frames.append(json.loads(message["data"]))
        types = [f["type"] for f in frames]
        assert types[0] == "run.started"
        assert types.count("run.output.delta") == 2
        assert types[-1] == "run.finished"
        assert frames[-1]["payload"]["status"] == "succeeded"

        async with migrated_engine.connect() as conn:
            titles = [
                r.title
                for r in await conn.execute(
                    text("SELECT title FROM tasks WHERE project_id = :p ORDER BY title"), {"p": pid}
                )
            ]
        assert titles == [
            "As a user, I want to add tasks, so that I track work",
            "As a user, I want to filter by status, so that I focus",
        ]
        await ps.aclose()
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_run_intake_failure_is_resilient(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        ps: Any = redis.pubsub()
        await ps.subscribe(channels.project_channel(str(pid)))
        await ps.get_message(timeout=0.05)

        script = [StreamDelta(type="text", text="partial")]
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, _FakeGateway(script, raises=True)),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="whatever",
            model_alias="local-default",
        )
        assert count == 0  # never raises
        frames = []
        for _ in range(8):
            message = await ps.get_message(timeout=0.1)
            if message and message["type"] == "message":
                frames.append(json.loads(message["data"]))
        assert frames[-1]["type"] == "run.finished"
        assert frames[-1]["payload"]["status"] == "failed"
        await ps.aclose()
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_run_intake_survives_over_length_title(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        long_title = "As a user, I want " + "x" * 800 + ", so that Y"
        payload = json.dumps(
            [
                {"title": long_title, "acceptance_criteria": ["ok"]},
                {"title": "As a user, I want Z, so that W"},
            ]
        )
        script = [
            StreamDelta(type="text", text=payload),
            StreamDelta(
                type="done", finish_reason="stop", usage=Usage(input_tokens=1, output_tokens=1)
            ),
        ]
        # One over-length title must not roll back the whole backlog: it is clamped
        # + persisted per-story, so BOTH stories land (review fix).
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, _FakeGateway(script)),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="b",
            model_alias="local-default",
        )
        assert count == 2
        async with migrated_engine.connect() as conn:
            n = (
                await conn.execute(
                    text("SELECT count(*) FROM tasks WHERE project_id = :p"), {"p": pid}
                )
            ).scalar()
        assert n == 2
        await redis.aclose()

    run_async(scenario)


def test_gateway_stream_metering_is_best_effort(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = tmp_path / "models.yaml"
        config.write_text(_CONFIG, encoding="utf-8")
        handler = _ollama_stream_handler(
            ["Hi ", "there"], {"prompt_tokens": 5, "completion_tokens": 3}
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = LlmGateway(ConfigLoader(config), client)

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("usage store down")

        gateway._record_stream = boom  # type: ignore[method-assign]
        req = ChatRequest(
            model_alias="local-default",
            messages=[Message(role="user", content="hi")],
            max_tokens=32,
            stream=True,
        )
        # A metering failure must NOT abort the already-streamed completion.
        deltas = [d async for d in gateway.stream(req)]
        assert "".join(d.text or "" for d in deltas if d.type == "text") == "Hi there"
        await client.aclose()

    run_async(scenario)
