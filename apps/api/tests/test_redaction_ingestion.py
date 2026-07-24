"""TITAN-SEC-055 DoD, ingestion half: a repo containing ``.env`` and
``id_rsa`` never surfaces them in assembled context OR in Qdrant.

Reality check baked into this test (Phase-69 scouting): TITAN's RAG pipeline
embeds Postgres memory chunks, never repo files — the ONLY way file bytes
enter the system is the tool layer. So the honest end-to-end proof is:

  1. assemble context exactly the way the platform does (fs.glob/list/read
     over the workspace) → the planted files are invisible;
  2. persist that context as a memory event and drive the REAL Phase-35
     pipeline (relay → embedder → Qdrant) with the REAL LlmGateway as the
     embeddings client (MockTransport records the provider wire) → nothing
     secret-bearing reaches the embeddings provider or the Qdrant store —
     including a provider-shaped key smuggled into the memory body itself
     (the ``terminal.run cat .env`` bypass path), which sink (a) redacts.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer
from titan.events.service import RedisStreamBus
from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.memory import service as memory
from titan.memory.ingest import (
    EMBEDDERS_GROUP,
    STREAM_MEMORY_INGEST,
    IngestRelay,
    MemoryEmbedder,
)
from titan.memory.qdrant import QdrantIndex
from titan.memory.service import MemoryEvent, MemoryEventBatch
from titan.tools.builtin import fs as fs_tools
from titan.tools.errors import ToolError
from titan.tools.registry import ToolExecutionContext
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager

pytestmark = pytest.mark.schema

_ENV_VALUE = "DB_PASSWORD=hunter2-dotenv-value-9000"
_RSA_VALUE = "planted-id-rsa-material"
_SMUGGLED_KEY = "sk-ant-api03-SmuggledViaTerminal01"

_CONFIG = """
providers:
  ollama: { base_url: http://ollama-test:11434 }
model_aliases:
  embed-default: { provider: ollama, model: nomic-embed-text, dimensions: 768 }
routing:
  default: embed-default
  embeddings_default: embed-default
"""


def _workspace(tmp_path: Path) -> Workspace:
    ws = WorkspaceManager(tmp_path / "root").for_call(uuid.uuid4(), uuid.uuid4())
    (ws.root / ".env").write_text(_ENV_VALUE, encoding="utf-8")
    (ws.root / "id_rsa").write_text(_RSA_VALUE, encoding="utf-8")
    (ws.root / "app.py").write_text("print('app')\n", encoding="utf-8", newline="")
    (ws.root / "README.md").write_text("run the app\n", encoding="utf-8", newline="")
    return ws


def _ctx(ws: Workspace) -> ToolExecutionContext:
    return ToolExecutionContext(
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        agent_id="agent.backend.1",
        agent_uuid=None,
        workspace=ws,
    )


async def _assemble_context(ws: Workspace) -> str:
    """Context assembly the way the platform does it: enumerate, then read
    every readable file — the exact tool surface an agent turn drives."""
    ctx = _ctx(ws)
    listing = await fs_tools._glob({"pattern": "**/*"}, ctx)
    pieces = [f"files: {', '.join(listing.result['matches'])}"]
    for rel in listing.result["matches"]:
        try:
            read = await fs_tools._read({"path": rel}, ctx)
        except ToolError:
            continue
        pieces.append(f"--- {rel} ---\n{read.result['content']}")
    return "\n".join(pieces)


def _real_gateway_pipeline(
    engine: AsyncEngine, tmp_path: Path, records: list[dict[str, Any]]
) -> tuple[IngestRelay, MemoryEmbedder, RedisStreamBus, FakeQdrantServer]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        records.append(body)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1] * 768}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    config = tmp_path / "models.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    gateway = LlmGateway(
        ConfigLoader(config), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    bus = RedisStreamBus(fakeredis.aioredis.FakeRedis(decode_responses=True))
    server = FakeQdrantServer()
    return (
        IngestRelay(engine, bus),
        MemoryEmbedder(engine, bus, QdrantIndex(server.client()), gateway),
        bus,
        server,
    )


def test_env_and_id_rsa_absent_from_context_and_qdrant(api: dict[str, Any], tmp_path: Path) -> None:
    engine: AsyncEngine = api["engine"]
    ws = _workspace(tmp_path)
    records: list[dict[str, Any]] = []
    relay, embedder, bus, server = _real_gateway_pipeline(engine, tmp_path, records)

    async def scenario() -> tuple[str, list[Any]]:
        # Neutralise outbox rows left by other tests on the shared DB.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET processed_at = now() WHERE processed_at IS NULL"
                )
            )
        await embedder.qdrant.ensure_collections()
        context = await _assemble_context(ws)
        # The realistic downstream: the assembled context (plus a secret that
        # slipped past filename filtering via `terminal.run cat .env`) becomes
        # durable memory and is embedded.
        batch = MemoryEventBatch(
            source_agent="memory_agent",
            events=[
                MemoryEvent(
                    kind="semantic",
                    scope="org",
                    title="workspace survey",
                    body_md=f"{context}\nterminal said: key={_SMUGGLED_KEY}",
                    metadata={"tags": ["phase69"]},
                )
            ],
        )
        async with engine.begin() as conn:
            await memory.write_events(conn, batch, source_agent="memory_agent", actor_agent="t")
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await relay.relay_once()
        deliveries = await bus.consume(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, "c1", block_ms=1)
        for delivery in deliveries:
            assert await embedder.handle(delivery)
        return context, deliveries

    context, deliveries = run_async(scenario)
    assert deliveries, "nothing flowed through the ingestion pipeline"

    # 1. Assembled context: the files and their content are simply absent.
    assert ".env" not in context
    assert "id_rsa" not in context
    assert _ENV_VALUE not in context
    assert _RSA_VALUE not in context
    assert "app.py" in context  # ordinary files still assemble

    # 2. The embeddings-provider wire (the REAL gateway sent it): no secrets.
    wire = json.dumps(records)
    assert records, "the embedder never called the embeddings provider"
    assert _ENV_VALUE not in wire
    assert _RSA_VALUE not in wire
    assert _SMUGGLED_KEY not in wire  # sink (a) caught the terminal bypass
    # The provider wire carries the CLASS-ONLY marker (no hash): the gateway sink
    # deliberately strips the hash8 correlator so it is never egressed to the LLM
    # provider (Phase-69 FIX 4 / _NO_CORRELATOR_SINKS). Internal sinks keep the
    # hash; this asserts the redaction fired AND the fingerprint did not leak.
    assert "[REDACTED:anthropic-key]" in wire

    # 3. Qdrant: points exist for the clean chunks, and NOTHING stored —
    #    ids, vectors, payloads — carries the planted values or filenames.
    points = server.points("titan_memory")
    assert points, "no points reached Qdrant"
    stored = json.dumps(points)
    assert _ENV_VALUE not in stored
    assert _RSA_VALUE not in stored
    assert _SMUGGLED_KEY not in stored
    assert ".env" not in stored
    assert "id_rsa" not in stored
