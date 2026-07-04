# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 5 — AI Brain: Memory, Knowledge & RAG

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform Specification |
| **Volume** | 5 of 10 — AI Brain: Memory, Knowledge & RAG |
| **Doc ID** | TITAN-VOL-05 |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |
| **Requirement prefix** | TITAN-BRN-NNN |

**Abstract.** This volume specifies the cognitive architecture of TITAN: the memory subsystem that lets an organization of AI agents remember, learn, and improve across tasks and projects. It defines the memory taxonomy (short-term/working, long-term, episodic, semantic, procedural; project vs organization scope), the exact mapping of each memory class onto PostgreSQL 16, Qdrant, and Redis 7, the embedding and retrieval pipelines (chunking, hybrid search, reranking, context assembly), a knowledge graph of entities and how it is maintained, context-window budget management with compression and summarization, pruning and decay policies, the Knowledge Base of coding standards and reusable patterns, cross-project learning, and the reflection loops that write experience back into memory. All designs run fully local via Docker Compose with free/open-source components (local embeddings via Ollama), consistent with the canonical stack fixed in Volume 1.

---

## Table of Contents

- 1. Introduction & Scope
- 2. Cognitive Architecture Overview
- 3. Memory Taxonomy
- 4. Storage Mapping: PostgreSQL vs Qdrant vs Redis
- 5. Embedding Pipeline
- 6. Retrieval Pipeline
- 7. Knowledge Graph of Entities
- 8. Context-Window Budget Management & Compression
- 9. Memory Pruning & Decay Policies
- 10. The Knowledge Base
- 11. Learning From Previous Projects
- 12. Reflection Loops Feeding Memory
- 13. Memory Flow Around One Agent Invocation
- 14. Memory Service API Surface
- 15. Risks & Open Questions
- 16. Cross-References

---

## 1. Introduction & Scope

### 1.1 Purpose

TITAN is an organization of agents, not one giant model. An organization without institutional memory re-solves the same problems forever. This volume defines the **Memory Service** — a single backend module (Python 3.12 / FastAPI / SQLAlchemy / Pydantic v2, per the canonical stack) that owns all durable cognition: what agents remember inside a task, across a project, and across the whole organization. The Memory Agent defined in Volume 2 is the sole steward of this service; every other agent reads and writes memory only through the APIs specified here.

### 1.2 In scope

Memory taxonomy and semantics; physical storage mapping; embedding and retrieval (RAG) pipelines; the knowledge graph; context budgeting, compression, and summarization; pruning and decay; the Knowledge Base; cross-project learning; reflection-driven memory writes; data schemas and flow diagrams.

### 1.3 Out of scope (owned elsewhere)

Agent role definitions and prompt templates (Volume 2); the message bus and task orchestration (Volumes 3 and 6); UI for the Memory Explorer (Volume 4); the LLM gateway internals (Volume 3); security controls on stored data (Volume 8); self-evolution metrics that consume memory (Volume 10).

### 1.4 Canonical constraints (fixed in Volume 1, referenced here)

PostgreSQL 16 is the system of record; Redis 7 provides cache, queues (Redis Streams at MVP), and pub/sub; Qdrant is the vector database; LLM and embedding access go through the provider-agnostic gateway (Anthropic, OpenAI, Google, Ollama), switchable by configuration only; everything must run on one laptop under Docker Compose with free/local tools; production targets Kubernetes; observability via OpenTelemetry/Prometheus/Grafana/Loki.

---

## 2. Cognitive Architecture Overview

The AI Brain is organized as three planes:

```
+---------------------------------------------------------------------+
|                          AGENT PLANE                                |
|  CEO / PM / Architect / Backend / Frontend / QA / Security / ...    |
|  (Volume 2)   reads "Memory Packets", emits "Memory Events"         |
+-------------------------------|-------------------------------------+
                                | Memory Service API (FastAPI, internal)
+-------------------------------v-------------------------------------+
|                         COGNITIVE PLANE                             |
|  +----------------+  +------------------+  +---------------------+  |
|  | Retrieval      |  | Ingestion        |  | Curation            |  |
|  | hybrid search, |  | chunking,        |  | reflection intake,  |  |
|  | rerank, packet |  | embedding,       |  | KG maintenance,     |  |
|  | assembly       |  | indexing         |  | pruning & decay     |  |
|  +----------------+  +------------------+  +---------------------+  |
+-------------------------------|-------------------------------------+
                                | SQLAlchemy / qdrant-client / redis-py
+-------------------------------v-------------------------------------+
|                          STORAGE PLANE                              |
|  PostgreSQL 16          Qdrant               Redis 7                |
|  system of record       vector index         working memory,       |
|  (canonical text,       (embeddings +        caches, ingestion     |
|  KG, KB, ledger)        payload filters)     queue (Streams)       |
+---------------------------------------------------------------------+
```

Two invariants govern everything below:

1. **PostgreSQL is canonical; Qdrant and Redis are derived.** Any vector or cache entry can be rebuilt from Postgres. Losing Qdrant or Redis loses no knowledge, only speed (see ADR-05-01).
2. **Agents never touch storage directly.** All reads return a typed *Memory Packet*; all writes are *Memory Events* validated by Pydantic v2 schemas. This keeps memory auditable ("every communication stored, nothing lost" — Volume 1 principle) and lets curation policies evolve without touching agent code.

---

## 3. Memory Taxonomy

TITAN distinguishes memory by **duration**, **kind**, and **scope**.

### 3.1 Definitions

| Memory class | Contains | Typical lifetime | Example |
|---|---|---|---|
| **Short-term / working** | The live context of one task attempt: current messages, scratch notes, tool outputs, partial plans | Minutes–hours; ends with the task attempt | Backend Agent's in-flight reasoning while implementing `POST /auth/login` |
| **Long-term** | Anything promoted for durable reuse; umbrella for episodic + semantic + procedural below | Months–forever (subject to decay, §9) | "Project Nova uses cursor pagination everywhere" |
| **Episodic** | Time-stamped records of *what happened*: task transcripts, decisions taken, review outcomes, incidents, retrospectives | Long, with decay | "2026-07-02: QA rejected PR #41 — flaky test caused by shared fixture" |
| **Semantic** | Distilled *facts and knowledge* independent of when learned: entity facts, API knowledge, architecture facts, glossary | Long, versioned | "Qdrant payload filtering supports keyword match and range conditions" |
| **Procedural** | *How to do things*: coding standards, runbooks, prompt recipes, workflow checklists, reusable templates | Long, versioned; the Knowledge Base (§10) is its curated core | "Checklist: adding a new Alembic migration safely" |

### 3.2 Scope: project memory vs organization memory

Every long-term record carries a scope:

- **Project memory** (`scope = project:<uuid>`): true only within one project — its requirements, decisions, component inventory, conventions, open risks. Isolated per project so unrelated projects never contaminate each other's retrieval.
- **Organization memory** (`scope = org`): validated across projects — patterns, lessons learned, coding standards, template library, provider quirks. Records enter org scope only via the promotion workflow in §11.3; agents cannot write org memory directly.

Retrieval always searches project scope plus org scope, never other projects' scopes (enforced by mandatory Qdrant payload filters and SQL predicates — see Volume 8 for the security rationale).

### 3.3 Requirements

**TITAN-BRN-001 (MUST)** — The Memory Service SHALL classify every stored record with exactly one `kind` ∈ {episodic, semantic, procedural} and one `scope` ∈ {org, project:<id>}, plus lifecycle metadata (created_at, last_accessed_at, access_count, decay_score).
*Acceptance:* Inserting a record without kind/scope fails Pydantic validation with HTTP 422; a database CHECK constraint rejects invalid enum values.

**TITAN-BRN-002 (MUST)** — Working memory SHALL live only in Redis under task-scoped keys and SHALL never be the sole copy of a decision, artifact, or lesson; anything durable must be written through a Memory Event before task completion.
*Acceptance:* Flushing Redis between two tasks does not change the outcome of a replayed retrieval-only integration test; the task-completion handler rejects completion if pending Memory Events remain unflushed.

**TITAN-BRN-003 (MUST)** — Retrieval SHALL be constrained to the requesting task's project scope plus org scope.
*Acceptance:* An integration test seeding two projects with distinctive markers shows zero cross-project leakage over 100 varied queries.

---

## 4. Storage Mapping: PostgreSQL vs Qdrant vs Redis

### 4.1 Placement matrix

| Data | PostgreSQL 16 | Qdrant | Redis 7 |
|---|---|---|---|
| Canonical memory records (full text, metadata) | ✔ source of truth | — | — |
| Embeddings of chunks | chunk rows (no vectors) | ✔ vectors + payload | — |
| Knowledge graph nodes/edges | ✔ | node label embeddings (for fuzzy entity lookup) | — |
| Knowledge Base documents & versions | ✔ | ✔ chunk vectors | hot-doc cache |
| Working memory (scratchpads, running summaries) | — | — | ✔ `wm:{task_id}:*`, TTL |
| Embedding/ingestion queue | outbox table | — | ✔ Stream `titan:memory:ingest` |
| Retrieval cache (query → packet) | — | — | ✔ TTL 300 s |
| Access/audit ledger of memory reads & writes | ✔ append-only | — | — |
| Rolling conversation summaries (checkpointed) | ✔ | — | ✔ live copy |

### 4.2 ADR-05-01 — PostgreSQL as canonical store; Qdrant strictly a derived index

- **Context.** Memory must survive years and be auditable; vectors must be re-computable when the embedding model changes; local-first operation requires simple backup/restore.
- **Decision.** All memory text and metadata are written to PostgreSQL first, inside the same transaction as an outbox row; an ingestion worker consumes the outbox via Redis Streams and upserts vectors into Qdrant. Qdrant point IDs equal the Postgres `chunk.id` (UUID), making reconciliation a set difference.
- **Alternatives considered.** (a) Qdrant as primary store for memories — rejected: weak transactional guarantees, no relational joins for the knowledge graph and ledger. (b) pgvector only, no Qdrant — attractive for simplicity, but rejected at MVP scale ceiling: Qdrant's HNSW performance, payload-filtered search, and named-vector support are needed for the hybrid pipeline in §6, and it is fixed canon from Volume 1. (c) Dual-write from the API process — rejected: partial-failure windows; the outbox pattern gives at-least-once delivery with idempotent upserts.
- **Trade-offs.** Slight indexing latency (target p95 < 5 s from write to searchable, TITAN-BRN-007); operational cost of one more container — acceptable, Qdrant runs in ~150 MB RAM locally.

### 4.3 PostgreSQL schema (memory core)

```sql
-- schema: brain
CREATE TABLE brain.memory_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL CHECK (kind IN ('episodic','semantic','procedural')),
    scope           TEXT NOT NULL,              -- 'org' | 'project:<uuid>'
    project_id      UUID REFERENCES core.projects(id),
    source_agent    TEXT NOT NULL,              -- e.g. 'qa_agent'
    source_task_id  UUID REFERENCES core.tasks(id),
    title           TEXT NOT NULL,
    body_md         TEXT NOT NULL,              -- canonical content, Markdown
    metadata        JSONB NOT NULL DEFAULT '{}',
    importance      REAL NOT NULL DEFAULT 0.5,  -- 0..1, set at write, updated by reflection
    decay_score     REAL NOT NULL DEFAULT 1.0,  -- recomputed nightly (§9)
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived','superseded','deleted')),
    superseded_by   UUID REFERENCES brain.memory_records(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tsv             TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', title || ' ' || body_md)) STORED
);
CREATE INDEX ON brain.memory_records USING GIN (tsv);          -- lexical leg of hybrid search
CREATE INDEX ON brain.memory_records (scope, kind, status);

CREATE TABLE brain.memory_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),    -- == Qdrant point id
    record_id   UUID NOT NULL REFERENCES brain.memory_records(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    embed_model TEXT,                                          -- e.g. 'nomic-embed-text-v1.5'
    embedded_at TIMESTAMPTZ,
    UNIQUE (record_id, seq)
);

CREATE TABLE brain.ingest_outbox (
    id          BIGSERIAL PRIMARY KEY,
    chunk_id    UUID NOT NULL REFERENCES brain.memory_chunks(id),
    op          TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

CREATE TABLE brain.memory_ledger (                              -- append-only audit
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_agent TEXT NOT NULL,
    task_id     UUID,
    action      TEXT NOT NULL CHECK (action IN ('read','write','promote','prune','supersede')),
    record_ids  UUID[] NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'
);
```

### 4.4 Qdrant collections

| Collection | Vector | Payload (indexed) | Purpose |
|---|---|---|---|
| `titan_memory` | 768-d cosine (named vector `dense`) | scope, kind, project_id, record_id, source_agent, created_at, tags[], decay_score | All long-term memory chunks |
| `titan_kb` | 768-d cosine | doc_type, version, scope, tags[], language | Knowledge Base chunks (§10) |
| `titan_kg_labels` | 768-d cosine | node_type, scope | Fuzzy entity resolution for the knowledge graph (§7) |

Collections are created with `on_disk_payload: true` and HNSW `m=16, ef_construct=128` — comfortable defaults for laptop-scale (≤ 5M points) with sub-50 ms search.

### 4.5 Redis keyspace (working memory & pipeline)

| Key pattern | Type | TTL | Content |
|---|---|---|---|
| `wm:{task_id}:scratch` | HASH | 24 h | Agent scratchpad fields (plan, notes, tool-state) |
| `wm:{task_id}:summary` | STRING | 24 h | Current rolling summary (also checkpointed to PG) |
| `wm:{task_id}:events` | LIST | 24 h | Pending Memory Events awaiting flush |
| `titan:memory:ingest` | STREAM | — | Chunk ingestion queue (consumer group `embedders`) |
| `rc:{scope}:{query_hash}` | STRING | 300 s | Cached Memory Packet (JSON) |

**TITAN-BRN-004 (MUST)** — A `rebuild-index` admin command SHALL fully reconstruct all Qdrant collections from PostgreSQL.
*Acceptance:* After deleting the Qdrant volume, running `titan memory rebuild-index` restores search results equivalent to pre-deletion on a golden query set (recall@10 ≥ 0.99 vs. baseline).

**TITAN-BRN-005 (MUST)** — Every memory read and write SHALL append a `memory_ledger` row identifying actor agent, task, action, and affected records.
*Acceptance:* The Memory Explorer (Volume 4) can display a complete access trail for any record; a test performing N reads/writes finds exactly N ledger rows.

---

## 5. Embedding Pipeline

### 5.1 Flow

```
Memory Event (write) --TX--> memory_records + memory_chunks + ingest_outbox
                                        |
                    outbox relay (async loop, 1 s poll)
                                        v
                     Redis Stream titan:memory:ingest
                                        |
                 consumer group "embedders" (1..N workers)
                                        v
        LLM Gateway /embeddings (Ollama local by default)
                                        v
             Qdrant upsert (point id = chunk id, payload from record)
                                        v
             mark chunk.embedded_at, outbox.processed_at
```

Workers are idempotent (upsert by chunk UUID), acknowledge only after Qdrant confirms, and use the Streams pending-entries list for crash recovery. Failures retry with exponential backoff (max 5, then dead-letter stream `titan:memory:ingest:dlq` with an alert via Prometheus).

### 5.2 Chunking strategy

Chunking is content-type aware; the writer declares `content_type` in the Memory Event.

| Content type | Splitter | Target size | Overlap |
|---|---|---|---|
| Markdown / prose (decisions, lessons, docs) | Heading-aware recursive splitter (split at `##`/`###`, then paragraphs) | 512 tokens | 64 tokens |
| Source code | Syntax-aware via tree-sitter: whole functions/classes; oversize bodies split at statement boundaries | 400 tokens | 0 (unit boundaries) |
| Conversations / transcripts | Turn-grouped: consecutive turns until budget | 512 tokens | 1 turn |
| Tables / schemas | Never split a row/DDL statement; group rows | 384 tokens | header repeated per chunk |

Every chunk is embedded with a contextual header prefix (`[{scope} | {kind} | {record title} | chunk {seq}/{n}]`) so isolated chunks remain interpretable to the encoder and reranker.

### 5.3 ADR-05-02 — Embedding model: local-first `nomic-embed-text-v1.5` via Ollama, provider-swappable

- **Context.** Local-first and free-tier-friendly are hard requirements; embeddings are the highest-volume LLM-adjacent workload; changing models later must not require code changes.
- **Decision.** Default embedding model is **nomic-embed-text-v1.5** (768-d, 8192-token context, Apache-2.0) served by Ollama and called through the provider-agnostic gateway's `/embeddings` route. Cloud alternatives (OpenAI `text-embedding-3-small`, Google `gemini-embedding-001`, Voyage via the Anthropic partnership) are selected purely by gateway configuration. The active model name is stamped on every chunk (`embed_model`); collections are dimension-locked, so a model change triggers a background re-embed into a new collection followed by an alias swap.
- **Alternatives considered.** `bge-m3` (multilingual, heavier, 1024-d — SHOULD-level option for non-English orgs); `all-MiniLM-L6-v2` (tiny and fast but 256-token context is too small for code chunks); cloud-only embeddings (rejected: violates local-first).
- **Trade-offs.** Local embedding throughput on a laptop (~200–800 chunks/min CPU) bounds ingestion speed — acceptable because ingestion is asynchronous; slightly lower retrieval quality than the best cloud embedders — mitigated by hybrid search and reranking (§6).

### 5.4 Requirements

**TITAN-BRN-006 (MUST)** — Embeddings SHALL be requested only through the LLM gateway; switching embedding provider/model SHALL require configuration changes only.
*Acceptance:* Changing `EMBEDDINGS_PROVIDER=ollama` to `openai` in `.env` and running the re-embed job requires zero code edits; grep shows no direct provider SDK imports in the Memory Service.

**TITAN-BRN-007 (MUST)** — p95 time from Memory Event commit to the chunk being searchable in Qdrant SHALL be ≤ 5 s under nominal local load (≤ 10 events/s).
*Acceptance:* Load test asserts the ingestion-lag histogram (exported via OpenTelemetry → Prometheus) meets the target.

**TITAN-BRN-008 (SHOULD)** — Chunking SHALL be syntax-aware for the languages TITAN generates (TypeScript, Python, SQL at minimum), never splitting inside a function signature or DDL statement.
*Acceptance:* Golden-file tests over representative sources show zero mid-signature splits.

---

## 6. Retrieval Pipeline

### 6.1 Stages

Every retrieval request (`query`, `scope`, `kinds[]`, `k`, `budget_tokens`) runs:

1. **Query shaping.** The caller's raw need (usually the task description plus the agent's stated information need) is normalized; for multi-part needs the Memory Service issues up to 3 sub-queries.
2. **Hybrid candidate generation** — run in parallel:
   - *Dense:* Qdrant search on `titan_memory` (and `titan_kb` when `kinds` includes procedural), payload-filtered by scope/kind/status, top-40 per sub-query.
   - *Lexical:* PostgreSQL full-text search on `memory_records.tsv` (`websearch_to_tsquery`, `ts_rank_cd`), top-40. This leg catches exact identifiers — error codes, function names, ticket IDs — that dense vectors miss.
3. **Fusion.** Reciprocal Rank Fusion: `score(d) = Σ 1/(60 + rank_i(d))` across all lists; deduplicate by `record_id`, keep top-24 chunks.
4. **Reranking.** Cross-encoder **bge-reranker-v2-m3** (open weights) scores (query, chunk) pairs; runs in-process in the Memory Service via `sentence-transformers` (CPU, ONNX int8 — ~30 ms per pair batch locally). Fallback when disabled (`RERANKER=off` for low-RAM laptops): LLM listwise rerank through the gateway using the cheapest configured model. Keep top-N until 85 % of `budget_tokens` is filled.
5. **Freshness/importance adjustment.** Final ordering key: `0.7·rerank_norm + 0.2·decay_score + 0.1·importance` — recent, important memories win ties.
6. **Packet assembly** (§6.2).

### 6.2 The Memory Packet

Retrieval returns a typed structure the orchestrator splices into the agent prompt (prompt template slots are defined in Volume 2):

```json
{
  "packet_id": "9f3c…",
  "assembled_for": {"agent": "backend_agent", "task_id": "…", "call_seq": 3},
  "sections": {
    "identity_context":   "…project one-pager + active conventions (always included)…",
    "procedural":         ["KB: FastAPI service checklist v4 (chunk 2/5)", "…"],
    "semantic":           ["Component map: auth-service owns /auth/*", "…"],
    "episodic":           ["2026-07-02 QA rejection of PR #41: …"],
    "working_summary":    "…rolling summary of this task so far…"
  },
  "citations": [{"record_id": "…", "chunk_id": "…", "score": 0.83}],
  "token_cost": 3480,
  "budget_tokens": 4000
}
```

Rules: (a) every included chunk carries its `record_id` so agents can cite memory in decisions and reviewers can audit provenance; (b) sections are ordered procedural → semantic → episodic because standards must dominate anecdotes when they conflict; (c) `identity_context` (project one-pager, §8.2) is non-evictable.

### 6.3 ADR-05-03 — Hybrid RRF + cross-encoder rerank instead of dense-only retrieval

- **Context.** Software-engineering memory is dense with exact symbols (`ERR_CONN_RESET`, `useAuthStore`, `TITAN-BRN-007`) where embedding similarity underperforms; local embedding models amplify this gap.
- **Decision.** Always fuse dense (Qdrant) and lexical (Postgres FTS) candidates with RRF, then rerank with a local cross-encoder.
- **Alternatives considered.** Dense-only (simplest, misses exact matches); Qdrant sparse vectors/BM42 (viable, but Postgres FTS is already free with the canonical stack and keeps the lexical leg transactionally fresh); dedicated search engine e.g. OpenSearch (rejected at MVP: one more stateful service on the laptop).
- **Trade-offs.** Two query legs add ~20–40 ms and code complexity; reranker costs ~200–400 MB RAM — mitigated by the `RERANKER=off` fallback.

### 6.4 Requirements

**TITAN-BRN-009 (MUST)** — Every agent LLM call SHALL receive its context through a Memory Packet assembled by the retrieval pipeline; ad-hoc raw dumps of history into prompts are prohibited.
*Acceptance:* Orchestrator code review + an integration assertion that each `agent_call` span (OpenTelemetry) links to exactly one `packet_id`.

**TITAN-BRN-010 (MUST)** — Retrieval SHALL meet p95 ≤ 800 ms end-to-end locally (query → packet) with reranker on, ≤ 400 ms with it off.
*Acceptance:* Benchmark suite over a 100k-chunk corpus on the reference laptop profile passes both targets.

**TITAN-BRN-011 (MUST)** — A retrieval evaluation harness with a versioned golden dataset (≥ 200 query→relevant-record pairs) SHALL run in CI, reporting recall@10 and nDCG@10; regressions > 5 % fail the build.
*Acceptance:* CI job exists, produces the two metrics, and a deliberately broken fusion weight fails it.

**TITAN-BRN-012 (SHOULD)** — Identical (scope, query) retrievals within 300 s SHALL be served from the Redis packet cache.
*Acceptance:* Second identical call returns in < 20 ms and increments a `cache_hit` counter.

---

## 7. Knowledge Graph of Entities

### 7.1 Purpose and model

Vectors answer "what is similar"; the graph answers "what is connected": which decision constrains which component, which pattern was applied in which projects, which lesson was learned from which incident. TITAN stores the graph relationally in PostgreSQL (ADR-05-04) with typed nodes and edges:

**Node types:** `project`, `component` (service/module/package), `decision` (ADR), `pattern` (reusable architecture/code pattern), `technology`, `agent`, `person` (human client/reviewer), `incident`, `lesson`, `kb_doc`.

**Edge types:** `PART_OF`, `DEPENDS_ON`, `DECIDED_FOR` (decision→project/component), `SUPERSEDES`, `APPLIES` (project/component→pattern), `DERIVED_FROM` (lesson→incident/task), `CONSTRAINS` (decision→component), `AUTHORED_BY`, `USES` (component→technology), `DOCUMENTED_IN`.

```sql
CREATE TABLE brain.kg_nodes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_type  TEXT NOT NULL,
    scope      TEXT NOT NULL,                  -- 'org' | 'project:<uuid>'
    name       TEXT NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}',
    record_id  UUID REFERENCES brain.memory_records(id),  -- backing prose, if any
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, node_type, name)
);
CREATE TABLE brain.kg_edges (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    src        UUID NOT NULL REFERENCES brain.kg_nodes(id) ON DELETE CASCADE,
    dst        UUID NOT NULL REFERENCES brain.kg_nodes(id) ON DELETE CASCADE,
    edge_type  TEXT NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}',
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to   TIMESTAMPTZ,                    -- non-null = historical edge
    UNIQUE (src, dst, edge_type, valid_from)
);
CREATE INDEX ON brain.kg_edges (src, edge_type);
CREATE INDEX ON brain.kg_edges (dst, edge_type);
```

Traversals use recursive CTEs capped at depth 3 (sufficient for "project → components → decisions → lessons" expansion during packet assembly).

### 7.2 ADR-05-04 — Relational graph in PostgreSQL, no dedicated graph database at MVP

- **Context.** Graph queries here are shallow (depth ≤ 3), write volume is low, and the laptop constraint penalizes every extra stateful service.
- **Decision.** Model the graph as `kg_nodes`/`kg_edges` in PostgreSQL with recursive CTEs; expose typed traversal endpoints; embed node names in `titan_kg_labels` for fuzzy entity resolution.
- **Alternatives considered.** Neo4j Community (best traversal ergonomics; rejected: JVM footprint, licensing friction for managed scale-out, extra service); Apache AGE extension (promising, MAY be adopted later — the API layer hides the change); storing relations only as JSONB (rejected: no referential integrity, unqueryable).
- **Trade-offs.** Deep analytics (centrality, community detection) are awkward in SQL — deferred to Volume 10's evolution analytics, which can export the graph offline.

### 7.3 Graph maintenance

The graph is maintained by three mechanisms, all owned by the Memory Agent's curation loop:

1. **Structured writes.** Workflow events (Volume 6) carry entity references: an approved ADR creates a `decision` node plus `DECIDED_FOR`/`CONSTRAINS` edges; scaffolding a service creates a `component` node; a retrospective creates `lesson` nodes with `DERIVED_FROM` edges. These are deterministic — no LLM involved.
2. **Extraction pass.** For unstructured episodic records (long transcripts), a nightly LLM extraction job proposes entities/edges as *candidates* with confidence; candidates ≥ 0.8 confidence that resolve to existing nodes are auto-linked, others queue for Memory Agent review.
3. **Entity resolution & hygiene.** New node names are embedded and searched against `titan_kg_labels`; cosine ≥ 0.92 with same node_type/scope triggers merge-or-confirm. Superseded decisions get `valid_to` stamped and a `SUPERSEDES` edge — the graph keeps history rather than deleting it.

**TITAN-BRN-013 (MUST)** — Approving an ADR, creating a component, or filing a lesson SHALL synchronously create/update the corresponding graph nodes and edges in the same workflow transaction.
*Acceptance:* Workflow integration tests assert node/edge existence immediately after each event type.

**TITAN-BRN-014 (SHOULD)** — Entity resolution SHALL prevent duplicate nodes: importing the same component name twice in a scope yields one node.
*Acceptance:* Duplicate-import test results in a single node with merged properties and a ledger entry.

---

## 8. Context-Window Budget Management & Compression

### 8.1 Token budgets per agent call

The orchestrator computes a **context budget** for every LLM call from the active model's window (known via the gateway's model registry). Default allocation for a working window `W` (e.g., W = 32k tokens for a local model; percentages hold for larger clouds models with absolute caps):

| Slot | Share of W | Cap | Evictable? |
|---|---|---|---|
| System + agent role prompt (Volume 2) | 8 % | 3k | No |
| Identity context (project one-pager + conventions) | 6 % | 2k | No |
| Task instruction + acceptance criteria | 8 % | 3k | No |
| Memory Packet (procedural/semantic/episodic) | 30 % | 12k | Yes — retrieval trims to fit |
| Working summary of current task | 10 % | 4k | Compressed, not dropped |
| Recent verbatim turns / tool outputs | 28 % | 12k | Oldest evicted first |
| Response reserve | 10 % | 8k | — |

### 8.2 Compression & summarization strategy

Three cooperating mechanisms:

1. **Rolling task summary.** After every agent turn, if verbatim history exceeds its slot, the oldest turns are folded into `wm:{task_id}:summary` by a cheap gateway model using a fixed schema: *Goal / Done so far / Open threads / Constraints discovered / Next step*. The summary is regenerated incrementally (previous summary + evicted turns), checkpointed to PostgreSQL every 5 folds so a Redis loss costs at most 5 turns of nuance.
2. **Hierarchical episode summaries.** On task completion, the transcript is distilled into a task summary (≤ 300 tokens); sprint completion distills task summaries into a sprint summary; project completion distills sprints into the **project one-pager** (≤ 800 tokens) that becomes the permanent `identity_context`. Each layer links down (`metadata.children[]`), so retrieval can zoom from one-pager to raw transcript when needed.
3. **Compression-on-retrieval.** If a reranked chunk set exceeds the packet budget, lowest-scoring chunks are replaced by their parent record's stored summary line rather than dropped silently, and the packet marks them `compressed: true` so the agent knows more detail is one query away.

**TITAN-BRN-015 (MUST)** — No LLM call SHALL be issued whose assembled prompt exceeds the model's context window minus the response reserve; the orchestrator SHALL compress or evict per §8.1 rather than fail.
*Acceptance:* Fuzz test with adversarially long tool outputs produces zero context-overflow errors from the gateway across 1,000 calls.

**TITAN-BRN-016 (MUST)** — Rolling summaries SHALL follow the fixed five-field schema and be checkpointed to PostgreSQL at least every 5 folds.
*Acceptance:* Schema-validation test on generated summaries; kill-Redis test loses ≤ 5 turns of summary detail.

**TITAN-BRN-017 (SHOULD)** — Summarization SHALL route to the cheapest configured gateway model class (`tier: utility`), never the premium reasoning tier.
*Acceptance:* Gateway logs over a test run show zero utility-class summarization requests on premium models.

---

## 9. Memory Pruning & Decay Policies

### 9.1 Decay scoring

A nightly curation job recomputes for every active record:

```
decay_score = w_r · exp(-Δdays / H(kind)) + w_u · min(1, access_count / 20) + w_i · importance
   w_r = 0.5, w_u = 0.3, w_i = 0.2
   Half-life H: episodic = 30 days, semantic = 120 days, procedural = 365 days
   Δdays = days since last_accessed_at (or created_at if never accessed)
```

Reads refresh `last_accessed_at` and increment `access_count`; reflection loops (§12) may raise `importance`. Decay influences retrieval ordering (§6.1 step 5) long before anything is removed.

### 9.2 Pruning tiers

| Tier | Trigger | Action | Reversible? |
|---|---|---|---|
| **Demote** | decay_score < 0.35 | Excluded from default retrieval (`status` stays active, payload flag `demoted`) unless the query explicitly requests deep search | Yes, automatic on access |
| **Archive** | decay_score < 0.15 for 30 consecutive days | `status='archived'`; vectors deleted from Qdrant; text retained in PostgreSQL; excluded from all retrieval except Memory Explorer | Yes (re-embed on restore) |
| **Compact** | Archived episodic records > 180 days | Body replaced by its stored summary; full body moved to cold table `brain.memory_bodies_cold` | Partially |
| **Delete** | Only via retention policy (Volume 8) or explicit human/admin action | Hard delete + ledger entry | No |

Protected classes are never auto-pruned: decision records, lessons learned, anything referenced by an active KG edge from an active project, and all Knowledge Base documents. Prune runs are dry-run-first: the job writes a plan to the ledger, and destructive tiers (Compact/Delete) require Memory Agent approval above 100 records per run.

**TITAN-BRN-018 (MUST)** — The nightly curation job SHALL recompute decay scores and apply Demote/Archive tiers automatically, logging every transition to the ledger.
*Acceptance:* Seeding records with back-dated timestamps and running the job produces the expected tier transitions and ledger rows.

**TITAN-BRN-019 (MUST)** — Decision records, lessons, KB documents, and KG-referenced records SHALL be exempt from automatic pruning.
*Acceptance:* Curation-job unit tests show protected records untouched at decay_score = 0.

**TITAN-BRN-020 (SHOULD)** — Accessing a demoted or archived record SHALL automatically restore it (re-embed if needed) and reset its decay clock.
*Acceptance:* Deep-search access to an archived record flips status to active and its chunks reappear in Qdrant within the TITAN-BRN-007 latency budget.

---

## 10. The Knowledge Base

### 10.1 Content types

The Knowledge Base (KB) is the curated, versioned core of procedural + semantic org memory:

| doc_type | Content | Primary writers | Primary readers |
|---|---|---|---|
| `coding_standard` | Language/framework rules (the "constitution", Volume 8 of the series governs content) | Human maintainers, Code Reviewer Agent proposals | Every engineer agent; Code Reviewer enforces |
| `architecture_pattern` | Named patterns with context/solution/consequences + reference implementation links | System Architect Agent | Architect, Backend, Frontend agents |
| `best_practice` | Focused guidance (e.g., "idempotent consumers on Redis Streams") | Any agent via proposal | All agents |
| `template` | Reusable scaffolds: service skeletons, Dockerfiles, CI configs, prompt fragments | DevOps/Backend/Frontend agents | Scaffolding steps in Volume 6 workflow |
| `decision_record` | Org-level ADRs and their history | Architect + CEO approval | All agents |
| `api_reference_note` | Distilled third-party API knowledge from the Research Agent | Research Agent | All agents |

### 10.2 Storage & versioning

```sql
CREATE TABLE brain.kb_documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_type    TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,          -- 'python-service-standard'
    title       TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated')),
    tags        TEXT[] NOT NULL DEFAULT '{}'
);
CREATE TABLE brain.kb_versions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES brain.kb_documents(id),
    version     INTEGER NOT NULL,
    body_md     TEXT NOT NULL,
    change_note TEXT NOT NULL,
    proposed_by TEXT NOT NULL,                 -- agent or human id
    approved_by TEXT,                          -- required before active
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version)
);
```

Only the approved current version is chunked into `titan_kb` for retrieval; older versions remain queryable by the Memory Explorer.

### 10.3 How agents read and write the KB

**Read path.** KB retrieval is part of the standard pipeline (§6): any packet request with `kinds` including `procedural` searches `titan_kb`. Additionally, workflow steps can pin documents deterministically — e.g., the code-review step (Volume 6) always pins the relevant `coding_standard` docs by slug, bypassing similarity search, because standards must be present even when the query doesn't resemble them.

**Write path — proposal workflow.** Agents never mutate the KB directly:

1. An agent submits a *KB proposal* (new doc or new version) with a diff and rationale, typically emitted by a reflection loop (§12).
2. The Memory Agent validates format, checks for conflicts with existing docs (semantic search for near-duplicates), and routes it: `coding_standard`/`decision_record` require Code Reviewer Agent approval plus human sign-off at MVP; other types require Code Reviewer Agent approval only.
3. On approval, a new `kb_versions` row becomes current, chunks are re-embedded, and a KG node/edge update records provenance (`kb_doc` node, `AUTHORED_BY`, `DERIVED_FROM` lesson if applicable).

**TITAN-BRN-021 (MUST)** — KB mutations SHALL occur only through the proposal→approval workflow, with every version retaining proposer, approver, and change note.
*Acceptance:* Direct-write API attempts return 403; the version history for any doc reconstructs its full provenance chain.

**TITAN-BRN-022 (MUST)** — Workflow steps SHALL be able to pin KB documents by slug into any Memory Packet, independent of similarity ranking.
*Acceptance:* A packet requested with `pins: ["python-service-standard"]` always contains that doc's chunks, verified by test.

---

## 11. Learning From Previous Projects

### 11.1 Pattern extraction

At each milestone completion, the Memory Agent runs the **pattern miner**:

1. Collect the project's decision records, component graph, and code-review outcomes since the last run.
2. LLM pass proposes candidate patterns: recurring structures ("JWT refresh handled via Redis allow-list"), recurring fixes, recurring rejections.
3. Candidates are checked against org memory for novelty (semantic search, threshold 0.85); novel ones become `pattern` records in *project* scope with links to their evidence (KG edges `APPLIES`, `DERIVED_FROM`).

### 11.2 Lessons-learned records

Every incident, rejected review with systemic cause, failed deployment, or retrospective finding produces a structured lesson:

```json
{
  "kind": "episodic", "record_subtype": "lesson",
  "title": "Shared pytest fixtures caused cross-test flakiness",
  "context": "Project Nova, sprint 3, QA regression run",
  "observation": "3 intermittent failures traced to a module-scoped DB fixture",
  "root_cause": "State leakage between tests via shared fixture",
  "recommendation": "Default to function-scoped fixtures; module scope requires justification",
  "evidence": ["task:…", "pr:41"],
  "applicability": "org-candidate",
  "importance": 0.8
}
```

### 11.3 Promotion: project memory → organization memory

A lesson or pattern is promoted to org scope when **any** of: (a) observed independently in ≥ 2 projects (the miner detects near-duplicates across project scopes); (b) flagged `org-candidate` and approved by the Memory Agent's weekly curation review; (c) a human marks it via the Memory Explorer. Promotion clones the record into org scope, links both via `SUPERSEDES`-style provenance edges, and — for procedural content — usually triggers a KB proposal (§10.3) so the lesson hardens into a standard or best practice. This is the pipeline by which TITAN's organization gets measurably better with every project.

**TITAN-BRN-023 (MUST)** — Milestone completion SHALL trigger pattern mining, and incidents/failed deployments SHALL each produce a structured lesson record within the same workflow run.
*Acceptance:* Workflow tests assert a lesson exists after a simulated failed deployment; miner runs are visible in the ledger.

**TITAN-BRN-024 (MUST)** — Promotion to org scope SHALL require the multi-signal rule above; agents SHALL NOT write org-scope records directly.
*Acceptance:* API rejects direct org-scope writes from agent credentials; a two-project duplicate lesson is auto-flagged for promotion in tests.

---

## 12. Reflection Loops Feeding Memory

Reflection is where the self-improvement loop (Implementation → Review → Critique → … , Volume 6) touches the brain. Three loops, increasing in scope:

1. **Task reflection (every task).** After the final review verdict, the acting agent answers a fixed rubric — *What was the goal? What worked? What surprised me? What would I do differently? What should the org remember?* — in ≤ 250 tokens. Output becomes an episodic record; the last field may spawn a lesson or KB proposal. Cost-bounded: utility model tier, one call.
2. **Sprint retrospective (every sprint).** The Project Manager Agent aggregates task reflections, review statistics, and blocker history; produces the sprint summary (§8.2), updated risk notes, and 0–3 lessons.
3. **Curation review (weekly, org level).** The Memory Agent reviews promotion candidates, KG extraction queue, prune plans, and retrieval-quality metrics (cache hit rate, packet citation rate — how often agents actually cite retrieved records in their outputs). Low-citation record classes get importance down-weighted; high-value classes up-weighted. This closes the loop: *retrieval quality itself is a learned quantity.*

**TITAN-BRN-025 (MUST)** — Every completed task SHALL produce a rubric-conformant reflection record before the task is marked done.
*Acceptance:* Task state machine (Volume 6) blocks the `done` transition without a linked reflection record; schema validated.

**TITAN-BRN-026 (SHOULD)** — Packet citation rate per record class SHALL be computed weekly and fed into importance weighting.
*Acceptance:* Metrics table `brain.retrieval_feedback` populates weekly; importance of a never-cited test class demonstrably decreases over 3 simulated weeks.

---

## 13. Memory Flow Around One Agent Invocation

The complete read/write cycle for a single agent call (e.g., Backend Agent implementing a task):

```
            ┌──────────────────────────────────────────────────────────────┐
            │                     ORCHESTRATOR (Vol 6)                     │
            │  task assigned to backend_agent, call_seq = N                │
            └───────┬──────────────────────────────────────────────────────┘
                    │ 1. request Memory Packet(task, agent, budget)
                    v
   ┌────────────────────────────┐   2a. dense top-40      ┌─────────────┐
   │      MEMORY SERVICE        │ ───────────────────────>│   QDRANT    │
   │  ┌──────────────────────┐  │ <───────────────────────│ titan_memory│
   │  │ RETRIEVAL             │ │   2b. lexical top-40    │ titan_kb    │
   │  │ shape → hybrid → RRF  │ │ ───────────┐            └─────────────┘
   │  │ → rerank → adjust     │ │            v
   │  │ → assemble packet     │ │      ┌─────────────┐    ┌─────────────┐
   │  └──────────────────────┘  │      │ POSTGRESQL  │    │   REDIS     │
   │  3. pins (KB slugs), KG    │─────>│ FTS, records│    │ wm:{task}:* │
   │     expansion depth ≤ 3    │      │ kg_*, kb_*  │    │ rc: cache   │
   │  4. + working summary ─────┼──────┼─────────────┼───>│             │
   └───────────┬────────────────┘      └─────────────┘    └─────────────┘
               │ 5. Memory Packet (typed, cited, ≤ budget)
               v
   ┌────────────────────────────┐  6. prompt = role + identity + task
   │        LLM GATEWAY         │     + packet + summary + recent turns
   │ (Anthropic/OpenAI/Google/  │  7. completion
   │  Ollama — config only)     │
   └───────────┬────────────────┘
               │ 8. agent output (code, decision, message, tool calls)
               v
   ┌────────────────────────────┐
   │  WRITE-BACK (same call)    │  9.  append turn → wm:{task}:scratch
   │                            │  10. fold old turns → rolling summary
   │  ORCHESTRATOR + MEMORY SVC │  11. emit Memory Events:
   │                            │      - episodic: decision made / review verdict
   │                            │      - semantic: fact discovered
   │                            │      - KG: component/decision edges
   │                            │  12. TX: memory_records + chunks + outbox
   │                            │  13. ledger rows (read packet_id, writes)
   └───────────┬────────────────┘
               │ 14. async: outbox → Redis Stream → embed (Ollama) → Qdrant
               v
      memory searchable for call_seq = N+1  (p95 ≤ 5 s, TITAN-BRN-007)
```

Steps 1–8 are synchronous within the agent call; 9–13 complete before the call returns; 14 is asynchronous but bounded by TITAN-BRN-007 so the *next* agent in a handoff can retrieve what this one just learned.

---

## 14. Memory Service API Surface

Internal REST endpoints (FastAPI, JWT service-auth per Volume 8; full OpenAPI in the repo):

| Method & path | Purpose |
|---|---|
| `POST /memory/packets` | Assemble a Memory Packet (query, scope, kinds, pins, budget) |
| `POST /memory/events` | Write Memory Events (batch, transactional) |
| `GET /memory/records/{id}` | Fetch canonical record + chunks + ledger trail |
| `POST /memory/kg/query` | Typed graph traversal (start nodes, edge types, depth ≤ 3) |
| `POST /kb/proposals` / `POST /kb/proposals/{id}/approve` | KB write workflow (§10.3) |
| `POST /memory/admin/rebuild-index` | Rebuild Qdrant from PostgreSQL (TITAN-BRN-004) |
| `POST /memory/admin/curation/run` | Trigger decay/prune/promotion cycle (also nightly cron) |
| `GET /memory/metrics` | Retrieval-quality and ingestion-lag metrics (also exported to Prometheus) |

All endpoints emit OpenTelemetry spans; packet assembly spans carry `packet_id`, candidate counts per leg, and rerank latency for Grafana dashboards.

---

## 15. Risks & Open Questions

| # | Risk / question | Impact | Mitigation / owner |
|---|---|---|---|
| R1 | Local embedding quality caps retrieval precision on niche code vocabulary | Agents miss relevant memories, redo work | Hybrid lexical leg (§6), reranker, eval harness TITAN-BRN-011 tracks it; config-only upgrade path to cloud embedders |
| R2 | Summarization drift: compounding rolling summaries lose critical constraints | Agent violates a forgotten constraint | Fixed five-field schema, constraints copied verbatim not paraphrased, checkpoint audits sampled in weekly curation |
| R3 | Memory poisoning: a wrong "fact" gets written and reinforces itself via retrieval | Systematic bad decisions | Provenance on every record, citation-required decisions, `supersede` workflow, reviewer agents can flag records; security review in Volume 8 |
| R4 | Reranker RAM (~400 MB) too heavy for minimal laptops | Local dev friction | `RERANKER=off` LLM-rerank fallback is a first-class tested mode |
| R5 | Nightly LLM extraction/curation cost at scale | Cost creep against free-tier goal | All curation on utility tier / local models; batch sizes capped; cost surfaced in metrics |
| Q1 | Should episodic half-life (30 d) differ per project phase (e.g., slower decay during active development)? | Tuning | Revisit after first longitudinal eval; parameterized per-scope already |
| Q2 | Adopt Qdrant sparse vectors (BM42) to replace the Postgres FTS leg once stable? | Simplification | Benchmark against golden set in a spike; decision deferred to v1.1 |
| Q3 | When multi-tenant SaaS arrives (Volume 10), does org memory need per-tenant partitioning beyond `scope`? | Architecture | Coordinate with Volume 8 tenancy model before GA |

---

## 16. Cross-References

- **Volume 1 — Foundation:** canonical stack decisions (PostgreSQL/Redis/Qdrant, gateway, local-first) that this volume builds on.
- **Volume 2 — Multi-Agent Intelligence:** the Memory Agent's role charter; per-agent prompt template slots that consume Memory Packets.
- **Volume 3 — Backend Engineering:** the LLM gateway, Redis Streams infrastructure, service auth, and deployment of the Memory Service.
- **Volume 4 — Frontend & Dashboard:** the Memory Explorer UI that renders records, the knowledge graph, ledger trails, and KB version history.
- **Volume 6 — Engineering Workflow:** task/sprint state machines that trigger reflections, pattern mining, and the write-back steps in §13.
- **Volume 8 — Security:** retention/deletion policy, scope-isolation guarantees, and threat treatment of memory poisoning (R3).
- **Volume 9 — Production:** backup/restore of PostgreSQL and Qdrant volumes; scaling embedder workers on Kubernetes.
- **Volume 10 — Autonomous Evolution:** self-evaluation metrics and cross-model debates that consume the episodic memory and lessons specified here.

*End of Volume 5 — TITAN-VOL-05, Draft v1.0.*
