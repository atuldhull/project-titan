"""Phase 25 (walking skeleton) tests: the story + clarification parsers, the direct
WS publisher, the gateway's metered streaming (usage lands in llm_usage), and the
CEO intake handler end to end against a fake gateway — including the §2.4
clarification batch it opens through the Phase-43 engine. Hermetic parts run over
fakeredis in a single loop; the persistence/metering parts are ``schema``-marked
(Postgres)."""

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

from titan.clarification import service as clarification
from titan.clarification.engine import MAX_CLARIFICATION_QUESTIONS
from titan.db import uuid7
from titan.intake.questions import MAX_PROPOSED_QUESTIONS, parse_clarification_draft
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


# --- clarification-question parser ------------------------------------------


def test_parse_clarification_draft_from_object_with_prose_and_fence() -> None:
    text_out = (
        "Sure! Here is what I need to know:\n```json\n"
        '{"questions": [{"topic": "content licensing", "question": "Real licensing or mocked?"},'
        '{"topic": "scale", "question": "How many users?"}],'
        '"inferable_topics": ["region", "team size"]}'
        "\n```\nLet me know."
    )
    draft = parse_clarification_draft(text_out)
    assert [(q.topic, q.question) for q in draft.questions] == [
        ("content licensing", "Real licensing or mocked?"),
        ("scale", "How many users?"),
    ]
    assert draft.inferable_topics == ("region", "team size")


def test_parse_clarification_draft_falls_back_to_bare_array() -> None:
    # A model that dropped the envelope: the first balanced `{…}` is the first ITEM,
    # not the envelope, so the object parse must decline and the array win.
    text_out = '[{"topic": "scope", "question": "How big?"}]'
    draft = parse_clarification_draft(text_out)
    assert [q.topic for q in draft.questions] == ["scope"]
    assert draft.inferable_topics == ()


def test_parse_clarification_draft_drops_untopiced_and_malformed() -> None:
    # No topic → the §2.4 policy could not check it against the canonical defaults,
    # so it is never asked. Same for a question with no text, and for non-objects.
    text_out = json.dumps(
        {
            "questions": [
                {"question": "Which cloud?"},
                {"topic": "scope", "question": "   "},
                "not an object",
                {"topic": "payments", "question": "Real payments or mocked?"},
            ]
        }
    )
    draft = parse_clarification_draft(text_out)
    assert [q.topic for q in draft.questions] == ["payments"]


def test_parse_clarification_draft_is_bounded() -> None:
    # The runaway guard — NOT the policy cap. The engine still owns the ≤5 decision.
    many = json.dumps({"questions": [{"topic": f"t{i}", "question": f"q{i}?"} for i in range(40)]})
    assert len(parse_clarification_draft(many).questions) == MAX_PROPOSED_QUESTIONS
    assert MAX_PROPOSED_QUESTIONS > MAX_CLARIFICATION_QUESTIONS


def test_parse_clarification_draft_clamps_over_length_fields() -> None:
    # Clamped to the clarification DTO's caps so a verbose model cannot raise a
    # validation error at submit time (mirrors the story title clamp).
    payload = json.dumps(
        {
            "questions": [{"topic": "t" * 400, "question": "q" * 4000}],
            "inferable_topics": ["i" * 400],
        }
    )
    draft = parse_clarification_draft(payload)
    assert len(draft.questions[0].topic) == 200 and len(draft.questions[0].question) == 2000
    assert len(draft.inferable_topics[0]) == 200


def test_parse_clarification_draft_empty_on_garbage() -> None:
    # An unparseable turn yields nothing to ask — a safe outcome, not a failure.
    assert parse_clarification_draft("I have no questions, let's build!") == (
        parse_clarification_draft("")
    )
    assert parse_clarification_draft("nope").questions == ()


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
    """A gateway double whose ``stream`` yields a scripted delta sequence. Intake
    makes TWO turns per run (clarification, then stories): ``turns`` scripts each
    successive call, while a bare ``script`` is replayed for every call. Requests
    are recorded so a test can assert which prompt drove which turn."""

    def __init__(
        self,
        script: list[StreamDelta] | None = None,
        *,
        turns: list[list[StreamDelta]] | None = None,
        raises: bool = False,
    ) -> None:
        self._script = script or []
        self._turns = list(turns) if turns is not None else None
        self._raises = raises
        self.calls: list[ChatRequest] = []

    async def stream(
        self, req: ChatRequest, *, run_id: uuid.UUID | None = None
    ) -> AsyncIterator[StreamDelta]:
        self.calls.append(req)
        script = self._turns.pop(0) if self._turns else self._script
        for delta in script:
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

_CLARIFY_JSON = json.dumps(
    {
        "questions": [{"topic": "sharing", "question": "Are task lists shared between users?"}],
        "inferable_topics": ["region"],
    }
)


def _story_script() -> list[StreamDelta]:
    return [
        StreamDelta(type="text", text=_STORY_JSON[:40]),
        StreamDelta(type="text", text=_STORY_JSON[40:]),
        StreamDelta(
            type="done", finish_reason="stop", usage=Usage(input_tokens=12, output_tokens=30)
        ),
    ]


def _clarify_script(payload: str = _CLARIFY_JSON) -> list[StreamDelta]:
    return [
        StreamDelta(type="text", text=payload),
        StreamDelta(
            type="done", finish_reason="stop", usage=Usage(input_tokens=8, output_tokens=9)
        ),
    ]


async def _clarification_events(engine: AsyncEngine, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """The clarification.updated events the engine wrote to the outbox for a project."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT payload FROM outbox "
                "WHERE payload->'data'->>'type' = 'clarification.updated' "
                "AND payload->'data'->'clarification'->>'project_id' = :p"
            ),
            {"p": str(project_id)},
        )
        raw = [r.payload for r in rows]
    return [cast(dict[str, Any], json.loads(p) if isinstance(p, str) else p) for p in raw]


@pytest.mark.schema
def test_run_intake_streams_and_persists(migrated_engine: AsyncEngine) -> None:
    """One run, two CEO turns: the clarification questions stream first, then the
    backlog — both under a single run.started/run.finished envelope."""

    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        ps: Any = redis.pubsub()
        await ps.subscribe(channels.project_channel(str(pid)))
        await ps.get_message(timeout=0.05)

        gateway = _FakeGateway(turns=[_clarify_script(), _story_script()])
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, gateway),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="Build a task tracker",
            model_alias="local-default",
        )
        assert count == 2
        assert len(gateway.calls) == 2  # clarification turn, then the backlog turn

        frames = []
        for _ in range(12):
            message = await ps.get_message(timeout=0.1)
            if message and message["type"] == "message":
                frames.append(json.loads(message["data"]))
        types = [f["type"] for f in frames]
        assert types[0] == "run.started"
        assert types.count("run.output.delta") == 3  # 1 clarification + 2 backlog
        # ONE envelope for the whole run: the UI's stream flips to done exactly once.
        assert types.count("run.started") == 1 and types.count("run.finished") == 1
        assert types[-1] == "run.finished"
        assert frames[-1]["payload"]["status"] == "succeeded"
        assert frames[-1]["payload"] == {
            "run_id": frames[-1]["payload"]["run_id"],
            "agent_id": "agent.ceo",
            "project_id": str(pid),
            "status": "succeeded",
            "stories": 2,
            "questions": 1,
        }
        # The question TEXT reaches the UI ONLY as run.output.delta tokens — this is
        # the sanctioned streaming path (TITAN-OPS-016).
        streamed = "".join(f["payload"]["delta"] for f in frames if f["type"] == "run.output.delta")
        assert "Are task lists shared between users?" in streamed

        async with migrated_engine.connect() as conn:
            titles = [
                r.title
                for r in await conn.execute(
                    text("SELECT title FROM tasks WHERE project_id = :p ORDER BY title"), {"p": pid}
                )
            ]
            batch = await clarification.get_clarification(conn, pid)
        assert titles == [
            "As a user, I want to add tasks, so that I track work",
            "As a user, I want to filter by status, so that I focus",
        ]
        # The batch was opened through the Phase-43 engine, by the CEO.
        assert batch.status == "open" and batch.opened_by == "agent.ceo"
        assert [q.question for q in batch.questions] == ["Are task lists shared between users?"]

        # clarification.updated is the UI's refetch signal and carries NO text.
        events = await _clarification_events(migrated_engine, pid)
        assert len(events) == 1
        emitted = events[0]["data"]["clarification"]
        assert emitted["question_count"] == 1 and emitted["status"] == "open"
        assert "Are task lists shared" not in json.dumps(events[0])

        await ps.aclose()
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_run_intake_lets_the_engine_own_the_policy(migrated_engine: AsyncEngine) -> None:
    """Intake proposes; ``clarification.engine`` disposes (TITAN-WFL-007). Intake
    applies neither the ≤5 cap nor the canonical-default/inferable demotion — it
    hands every candidate to the engine and the engine's verdict is what persists."""

    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        proposed = (
            # canonical default → demoted to an assumption, never asked
            [{"topic": "which database", "question": "Which database?"}]
            # inferable → demoted to an assumption, never asked
            + [{"topic": "region", "question": "Which region?"}]
            # 6 essential → 5 kept, 1 deferred as a high-visibility assumption
            + [{"topic": f"t{i}", "question": f"q{i}?"} for i in range(6)]
        )
        payload = json.dumps({"questions": proposed, "inferable_topics": ["region"]})
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, _FakeGateway(turns=[_clarify_script(payload), _story_script()])),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="Build a bookmarking app",
            model_alias="local-default",
        )
        assert count == 2  # the backlog still lands

        async with migrated_engine.connect() as conn:
            batch = await clarification.get_clarification(conn, pid)
        assert len(batch.questions) == MAX_CLARIFICATION_QUESTIONS
        assert not any("database" in q.topic for q in batch.questions)
        sources = {a.source for a in batch.assumptions}
        assert {"canonical_default", "org_memory", "excess_question"} <= sources
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_run_intake_skips_clarification_when_a_batch_exists(migrated_engine: AsyncEngine) -> None:
    """The single-batch policy: clarification_batches is UNIQUE on project_id, so a
    second submit is a hard 409. Intake must degrade — not crash — and still deliver
    the backlog, leaving the existing batch untouched."""

    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        await clarification.submit_user_request(
            migrated_engine,
            pid,
            clarification.UserRequestSubmission(
                raw_text="The original request",
                proposed_questions=[
                    clarification.ProposedQuestionIn(topic="scope", question="How big?")
                ],
            ),
            submitted_by="user.owner",
        )

        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(Any, _FakeGateway(turns=[_clarify_script(), _story_script()])),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="Build a second thing entirely",
            model_alias="local-default",
        )
        assert count == 2  # intake survived the 409 and delivered the backlog

        async with migrated_engine.connect() as conn:
            batch = await clarification.get_clarification(conn, pid)
        # The pre-existing batch is untouched — no second batch, no extra questions.
        assert batch.opened_by == "user.owner"
        assert [q.question for q in batch.questions] == ["How big?"]
        assert len(await _clarification_events(migrated_engine, pid)) == 1  # only the first open
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_run_intake_survives_unparseable_clarification_turn(migrated_engine: AsyncEngine) -> None:
    """A wonky clarification completion costs the project its questions, never its
    backlog: nothing essential parses, so the engine opens a question-less batch
    that resolves at once rather than lingering to the deadline."""

    async def scenario() -> None:
        redis = _fake()
        pid = uuid7()
        await _seed_project(migrated_engine, pid)
        count = await run_intake(
            engine=migrated_engine,
            gateway=cast(
                Any,
                _FakeGateway(turns=[_clarify_script("I have no questions!"), _story_script()]),
            ),
            publisher=RealtimePublisher(redis),
            project_id=pid,
            brief="Build a wiki",
            model_alias="local-default",
        )
        assert count == 2

        async with migrated_engine.connect() as conn:
            batch = await clarification.get_clarification(conn, pid)
        assert batch.questions == []
        assert batch.status == "answered" and batch.resolution == "no_questions"
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
