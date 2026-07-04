# PROJECT TITAN — Autonomous Software Engineering Platform

**Volume 3 — Backend Engineering & Infrastructure**

| Field | Value |
|---|---|
| Doc ID | TITAN-VOL-03 |
| Status | Draft v1.0 |
| Date | 2026-07-04 |
| Requirement prefix | TITAN-BCK-NNN |
| Audience | Backend engineers, platform engineers, AI coding agents implementing the TITAN core |

**Abstract.** This volume specifies the TITAN backend: a Python 3.12 + FastAPI (async) modular monolith that serves as the system of record, API surface, realtime event hub, and task-orchestration engine for the multi-agent platform. It defines the module boundaries and their extraction path to services, REST and WebSocket contracts, the PostgreSQL 16 schema, Redis 7 usage (cache, Redis Streams queues, pub/sub), the task orchestration state machine, authentication (OAuth2/OIDC + JWT) and RBAC, rate limiting, caching, observability hooks, and the Docker Compose local-development topology. Everything here is implementable directly, locally, on one laptop with free tools.

---

## Table of Contents

1. [Scope & Relationship to Other Volumes](#1-scope--relationship-to-other-volumes)
2. [Service Architecture](#2-service-architecture)
3. [Module Breakdown](#3-module-breakdown)
4. [REST API Design](#4-rest-api-design)
5. [WebSocket Event Protocol](#5-websocket-event-protocol)
6. [PostgreSQL Schema](#6-postgresql-schema)
7. [Redis Usage](#7-redis-usage)
8. [Task Orchestration Engine](#8-task-orchestration-engine)
9. [API Gateway, Authentication & RBAC](#9-api-gateway-authentication--rbac)
10. [Rate Limiting](#10-rate-limiting)
11. [Caching Strategy](#11-caching-strategy)
12. [Logging & Observability Hooks](#12-logging--observability-hooks)
13. [Local Development: Docker Compose & Repository Layout](#13-local-development-docker-compose--repository-layout)
14. [Risks & Open Questions](#14-risks--open-questions)
15. [Cross-References](#15-cross-references)

---

## 1. Scope & Relationship to Other Volumes

This volume owns the **backend/orchestrator process and its infrastructure**: HTTP/WebSocket surface, persistence, queues, orchestration, auth, and local-dev topology. It does **not** re-justify the canonical stack (Volume 1 owns that): monorepo; Next.js 15 + React + TypeScript + Tailwind + shadcn/ui frontend; Python 3.12 + FastAPI (async) + SQLAlchemy + Pydantic v2 backend; PostgreSQL 16 as system of record; Redis 7 for cache/queues/pub-sub; Qdrant for vectors; Redis Streams for the MVP task/agent queue with NATS JetStream as the scale-out option; WebSockets served by FastAPI; provider-agnostic LLM gateway (Anthropic, OpenAI, Google, Ollama) switchable by configuration only; Docker Compose locally, Kubernetes in production; OpenTelemetry + Prometheus + Grafana + Loki observability; OAuth2/OIDC + JWT with RBAC; open source, local-first, free-tier-friendly.

Agent role definitions live in Volume 2; memory/RAG internals in Volume 5; the end-to-end development workflow in Volume 6; plugin/tool integrations in Volume 7; deep security in Volume 8; production deployment in Volume 9.

---

## 2. Service Architecture

### 2.1 Topology

TITAN's backend at MVP is **one deployable FastAPI application** (`titan-core`) plus **one worker process image** (`titan-worker`) that shares the same codebase but runs stream consumers instead of the HTTP server. Both containers import the same Python package; the entrypoint differs.

```
                        ┌──────────────────────────────────────────────┐
                        │              titan-core (FastAPI)            │
  Next.js UI ──HTTP──▶  │  /api/v1/*  REST        ┌──────────────────┐ │
  Next.js UI ──WS────▶  │  /ws        WebSocket   │  Gateway layer   │ │
                        │                         │ auth·rbac·rate   │ │
                        │  ┌───────────────────┐  └──────────────────┘ │
                        │  │ Domain modules     │                      │
                        │  │ projects tasks     │──SQLAlchemy──▶ PostgreSQL 16
                        │  │ agents runs msgs   │──redis-py────▶ Redis 7
                        │  │ memory plugins     │──qdrant-client▶ Qdrant
                        │  └───────────────────┘                      │
                        └───────────────┬──────────────────────────────┘
                                        │ Redis Streams (tasks.ready, runs.execute)
                        ┌───────────────▼──────────────────────────────┐
                        │            titan-worker (N replicas)         │
                        │  stream consumers → agent runtime → LLM      │
                        │  gateway (Anthropic/OpenAI/Google/Ollama)    │
                        └──────────────────────────────────────────────┘
```

**TITAN-BCK-001 (MUST)** — The backend SHALL be implemented as a single FastAPI application organized as a modular monolith with the modules listed in §3.
*Acceptance:* `uvicorn titan.main:app` starts the full API; each module lives in its own package with no cross-module imports except via the module's public `service.py` interface; an import-linter contract in CI fails on boundary violations.

**TITAN-BCK-002 (MUST)** — All I/O paths (HTTP handlers, DB access, Redis, LLM calls) SHALL be async (`asyncpg` via SQLAlchemy 2.0 async engine, `redis.asyncio`).
*Acceptance:* no synchronous DB/Redis driver appears in `pyproject.toml`; blocking calls in handlers are flagged by a CI check running `flake8-async`.

**TITAN-BCK-003 (MUST)** — Worker processes SHALL run from the same image with entrypoint `python -m titan.worker`, consuming Redis Streams consumer groups.
*Acceptance:* killing a worker mid-task causes the message to be re-delivered to another consumer via `XAUTOCLAIM` within 60 s.

### 2.2 ADR-BCK-01 — Modular monolith now, services later

- **Context.** TITAN must run fully on one laptop (hard requirement) yet be architected so components can scale independently once real load exists (many concurrent agent runs are LLM-bound, not CPU-bound).
- **Decision.** Ship a modular monolith with enforced module boundaries (import-linter), a shared Postgres schema partitioned by module ownership (each table has exactly one owning module; other modules read via service interfaces, never raw table access), and all inter-module *asynchronous* interaction via Redis Streams events rather than in-process calls.
- **Alternatives considered.** (a) Microservices from day one — rejected: 8+ services on a laptop wastes RAM, multiplies local-dev pain, and the domain boundaries are not yet proven. (b) Single unstructured app — rejected: extraction later becomes a rewrite.
- **Trade-offs.** We accept one shared DB and one deploy unit at MVP (coarser failure domain) in exchange for laptop-friendliness and iteration speed. Because module communication already flows through events + service interfaces, extracting a module (first candidates: `agent-runtime` worker, then `memory`) means: give it its own DB schema/database, replace in-process service calls with HTTP/NATS calls behind the same interface, and move its stream consumers. The event contract does not change. NATS JetStream replaces Redis Streams at that point if throughput or durability demands it (see ADR-BCK-02).

---

## 3. Module Breakdown

| Module | Owns (tables) | Responsibility | Extraction priority |
|---|---|---|---|
| `gateway` | — | Routing, authN/Z, rate limits, request ID, CORS, OpenAPI | never (stays with API) |
| `identity` | users, roles, user_roles, api_keys | OAuth2/OIDC flows, JWT issuing/verification, RBAC checks | low |
| `projects` | projects, milestones, sprints | Project lifecycle, roadmap data | low |
| `orchestrator` | tasks, task_dependencies | Task state machine, assignment, prioritization, scheduling | medium |
| `agents` | agents | Agent registry: role, config, model binding, status | medium |
| `runs` | runs, artifacts | Execution records of agent work; artifact metadata | **high** (worker) |
| `messaging` | messages, decisions | Structured inter-agent messages, decision log | medium |
| `memory` | memory_items (+ Qdrant collections) | Memory API façade over Postgres + Qdrant (details: Vol 5) | **high** |
| `plugins` | plugins, plugin_installs | Plugin registry, manifest validation, lifecycle hooks (Vol 7) | medium |
| `llm` | — | Provider-agnostic LLM gateway client; config-driven provider switch | high |
| `events` | — | Outbox publisher, Redis Streams producers/consumers, WS fan-out | never (library) |
| `observability` | — | OTel setup, structlog config, Prometheus metrics registry | never (library) |

**TITAN-BCK-004 (MUST)** — Each domain module SHALL expose exactly one public interface (`<module>/service.py` with Pydantic v2 DTOs) and keep routers, repositories, and models private.
*Acceptance:* import-linter "independence" contracts pass; DTOs (not SQLAlchemy models) cross module boundaries.

**TITAN-BCK-005 (MUST)** — State changes that other modules react to SHALL be published as domain events through a transactional outbox (`outbox` table written in the same DB transaction, drained to Redis Streams by a background publisher).
*Acceptance:* killing the process between commit and publish never loses an event; the publisher is idempotent via event UUIDs.

---

## 4. REST API Design

### 4.1 Conventions

**TITAN-BCK-006 (MUST)** — The API SHALL follow these conventions. *Acceptance:* Schemathesis run against the OpenAPI spec passes; a spectral ruleset encoding the rules below passes in CI.

| Convention | Rule |
|---|---|
| Base path | `/api/v1` (URI versioning; breaking changes bump the segment) |
| Resource naming | plural kebab-case nouns: `/projects`, `/tasks`, `/plugin-installs` |
| IDs | UUIDv7 everywhere (time-ordered, index-friendly) |
| Body casing | `snake_case` JSON (Pydantic native); frontend maps at its boundary |
| Pagination | cursor-based: `?limit=50&cursor=<opaque>`; response carries `next_cursor` |
| Filtering/sort | `?status=in_progress&assignee_id=…&sort=-created_at` |
| Errors | RFC 9457 Problem Details: `{"type","title","status","detail","instance","errors":[…]}` |
| Idempotency | mutating POSTs accept `Idempotency-Key` header, replayed for 24 h from Redis |
| Concurrency | updates require `If-Match: <etag>` (row `version` column) → `412` on conflict |
| Timestamps | RFC 3339 UTC (`2026-07-04T12:00:00Z`) |
| Envelope | none for single resources; lists use `{"items":[…],"next_cursor":…}` |

### 4.2 Endpoint map

| Resource | Endpoints (all under `/api/v1`) |
|---|---|
| Projects | `POST /projects` · `GET /projects` · `GET /projects/{id}` · `PATCH /projects/{id}` · `POST /projects/{id}:archive` · `GET /projects/{id}/board` (kanban) · `GET /projects/{id}/graph` (dependency graph) |
| Tasks | `POST /projects/{id}/tasks` · `GET /projects/{id}/tasks` · `GET /tasks/{id}` · `PATCH /tasks/{id}` · `POST /tasks/{id}:transition` · `POST /tasks/{id}/dependencies` |
| Agents | `GET /agents` · `POST /agents` · `GET /agents/{id}` · `PATCH /agents/{id}` · `POST /agents/{id}:pause` / `:resume` |
| Runs | `POST /tasks/{id}/runs` · `GET /runs/{id}` · `GET /runs?task_id=…` · `POST /runs/{id}:cancel` · `GET /runs/{id}/artifacts` |
| Messages | `GET /projects/{id}/messages?thread_id=…&kind=…` · `POST /projects/{id}/messages` (human → agent) |
| Decisions | `GET /projects/{id}/decisions` · `POST /projects/{id}/decisions` |
| Memory | `POST /memory/search` · `POST /memory/items` · `GET /memory/items/{id}` · `DELETE /memory/items/{id}` |
| Plugins | `GET /plugins` · `POST /plugins/{id}:install` · `POST /plugins/{id}:enable` / `:disable` |
| Auth | `POST /auth/token` · `POST /auth/refresh` · `GET /auth/me` · `POST /auth/api-keys` |

Custom actions use the `:verb` suffix (Google AIP-136 style) so the state machine (§8) is the only way to move task state — no free-form `PATCH status`.

### 4.3 Example payloads

`POST /api/v1/projects`

```json
{
  "name": "netflix-clone",
  "display_name": "Netflix Clone",
  "brief": "Streaming platform with auth, catalog, playback, recommendations.",
  "mode": "autonomous",
  "llm_profile": "default-local"
}
```

`201 Created`

```json
{
  "id": "0197a3b2-7c1e-7d3a-9f00-1b2c3d4e5f60",
  "name": "netflix-clone",
  "display_name": "Netflix Clone",
  "status": "intake",
  "mode": "autonomous",
  "llm_profile": "default-local",
  "created_by": "0197a3b2-0000-7000-8000-000000000001",
  "created_at": "2026-07-04T09:12:33Z",
  "version": 1
}
```

`POST /api/v1/tasks/{id}:transition`

```json
{ "action": "submit_review", "actor_agent_id": "0197…be", "comment": "Backend auth module ready; 42 tests green." }
```

`200 OK`

```json
{
  "id": "0197a3c1-…",
  "title": "Implement JWT auth endpoints",
  "status": "in_review",
  "previous_status": "in_progress",
  "assignee_agent_id": "0197…be",
  "reviewer_agent_id": "0197…cr",
  "priority": 2,
  "estimate_points": 3,
  "sprint_id": "0197…sp",
  "version": 6
}
```

`POST /api/v1/memory/search`

```json
{ "query": "how did we structure JWT refresh last project?", "scope": "organization", "top_k": 5, "kinds": ["decision", "code_pattern"] }
```

`200 OK` → `{"items":[{"id":"…","kind":"decision","score":0.87,"summary":"Refresh tokens rotate on use…","source_ref":"decisions/0196…"}],"next_cursor":null}`

Error example (`409` on illegal transition), RFC 9457:

```json
{
  "type": "https://titan.dev/errors/invalid-transition",
  "title": "Invalid task transition",
  "status": 409,
  "detail": "Action 'complete' is not allowed from status 'backlog'.",
  "instance": "/api/v1/tasks/0197a3c1-…:transition"
}
```

---

## 5. WebSocket Event Protocol

**TITAN-BCK-007 (MUST)** — FastAPI SHALL serve `wss://…/ws?token=<jwt>`; after connect the client sends a `subscribe` frame per project/topic; the server fans out events published on Redis pub/sub channel `ws.project.{project_id}`.
*Acceptance:* two browser tabs subscribed to the same project both receive a `task.updated` frame within 250 ms (local) of the transition commit.

All frames are JSON with a fixed envelope:

```json
{
  "v": 1,
  "type": "run.output.delta",
  "seq": 18234,
  "ts": "2026-07-04T09:15:02.412Z",
  "project_id": "0197a3b2-…",
  "payload": { "run_id": "0197a3d9-…", "agent_id": "0197…be", "delta": "def create_app() ->" }
}
```

Client → server frames: `subscribe {project_id, topics[]}`, `unsubscribe`, `ping`. Server → client event types:

| Type | Payload core | Emitted when |
|---|---|---|
| `task.created` / `task.updated` | task DTO | orchestrator commits a change |
| `run.started` / `run.finished` | run DTO (status, cost, tokens) | worker begins/ends a run |
| `run.output.delta` | `{run_id, delta}` | streaming LLM output (live agent activity) |
| `message.created` | message DTO | any inter-agent or human message |
| `decision.recorded` | decision DTO | Decision log entry written |
| `agent.status` | `{agent_id, state, current_task_id}` | agent idle/busy/error/paused |
| `sprint.updated` | sprint DTO | planning changes |
| `sys.notice` | `{level, text}` | rate-limit warnings, provider fallback, etc. |

`seq` is a per-connection monotonically increasing integer; after reconnect the client sends `subscribe` with `last_seq`, and the server replays up to 1,000 buffered events per project from a Redis list (`ws.buffer.{project_id}`, capped, TTL 15 min) or instructs `{"type":"sys.resync"}` telling the client to refetch via REST. Heartbeat: server pings every 25 s; connection closed after 2 missed pongs.

---

## 6. PostgreSQL Schema

PostgreSQL 16 is the system of record. Migrations are Alembic, one head, autogenerate forbidden in CI (hand-reviewed migrations only).

**TITAN-BCK-008 (MUST)** — The core schema below SHALL be created by initial migrations; every table gets `created_at timestamptz NOT NULL DEFAULT now()`, `updated_at` (trigger-maintained), and optimistic-lock `version int NOT NULL DEFAULT 1`.
*Acceptance:* `alembic upgrade head` on empty Postgres 16 produces exactly these tables/indexes; `pytest -m schema` asserts them.

```sql
-- identity ---------------------------------------------------------------
CREATE TABLE users (
  id            uuid PRIMARY KEY,                  -- UUIDv7
  email         citext UNIQUE NOT NULL,
  display_name  text NOT NULL,
  auth_provider text NOT NULL,                     -- 'oidc:google' | 'oidc:github' | 'local'
  auth_subject  text NOT NULL,                     -- OIDC 'sub'
  is_active     boolean NOT NULL DEFAULT true,
  UNIQUE (auth_provider, auth_subject)
);

CREATE TABLE roles (
  id    uuid PRIMARY KEY,
  name  text UNIQUE NOT NULL,                      -- owner|admin|maintainer|operator|viewer
  permissions jsonb NOT NULL                        -- ["project:write","task:transition",...]
);

CREATE TABLE user_roles (
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role_id    uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  project_id uuid NULL REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = org-wide
  PRIMARY KEY (user_id, role_id, project_id)
);

-- projects ---------------------------------------------------------------
CREATE TABLE projects (
  id           uuid PRIMARY KEY,
  name         text UNIQUE NOT NULL,               -- slug
  display_name text NOT NULL,
  brief        text NOT NULL,
  status       text NOT NULL DEFAULT 'intake',     -- intake|planning|building|reviewing|released|archived
  mode         text NOT NULL DEFAULT 'autonomous', -- autonomous|supervised
  llm_profile  text NOT NULL DEFAULT 'default',
  created_by   uuid NOT NULL REFERENCES users(id)
);
CREATE INDEX idx_projects_status ON projects(status);

CREATE TABLE milestones (
  id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title text NOT NULL, due_at timestamptz, status text NOT NULL DEFAULT 'open'
);

CREATE TABLE sprints (
  id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  number int NOT NULL, goal text, starts_at timestamptz, ends_at timestamptz,
  status text NOT NULL DEFAULT 'planned',          -- planned|active|closed
  UNIQUE (project_id, number)
);

-- agents -------------------------------------------------------------------
CREATE TABLE agents (
  id           uuid PRIMARY KEY,
  project_id   uuid REFERENCES projects(id) ON DELETE CASCADE,   -- NULL = org-level template
  role         text NOT NULL,        -- ceo|pm|project_manager|architect|backend|frontend|db|devops|qa|security|reviewer|docs|research|memory
  name         text NOT NULL,
  status       text NOT NULL DEFAULT 'idle',       -- idle|busy|paused|error
  model_binding jsonb NOT NULL,      -- {"provider":"ollama","model":"qwen2.5-coder:14b","temperature":0.2}
  config       jsonb NOT NULL DEFAULT '{}',        -- prompt template ref, tool allowlist (Vol 2)
  current_task_id uuid NULL
);
CREATE INDEX idx_agents_project_role ON agents(project_id, role);

-- orchestration --------------------------------------------------------------
CREATE TABLE tasks (
  id            uuid PRIMARY KEY,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  sprint_id     uuid REFERENCES sprints(id),
  milestone_id  uuid REFERENCES milestones(id),
  parent_id     uuid REFERENCES tasks(id),          -- epics → subtasks
  title         text NOT NULL,
  spec          text NOT NULL DEFAULT '',           -- full task specification (markdown)
  status        text NOT NULL DEFAULT 'backlog',    -- backlog|ready|assigned|in_progress|in_review|changes_requested|done|reopened|cancelled
  priority      smallint NOT NULL DEFAULT 3,        -- 1 critical … 5 trivial
  estimate_points smallint,
  assignee_agent_id uuid REFERENCES agents(id),
  reviewer_agent_id uuid REFERENCES agents(id),
  created_by_agent_id uuid REFERENCES agents(id),
  completed_at  timestamptz
);
CREATE INDEX idx_tasks_project_status_priority ON tasks(project_id, status, priority);
CREATE INDEX idx_tasks_assignee ON tasks(assignee_agent_id) WHERE status IN ('assigned','in_progress');

CREATE TABLE task_dependencies (
  task_id       uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on_id),
  CHECK (task_id <> depends_on_id)
);

-- execution ------------------------------------------------------------------
CREATE TABLE runs (
  id          uuid PRIMARY KEY,
  task_id     uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  agent_id    uuid NOT NULL REFERENCES agents(id),
  attempt     int  NOT NULL DEFAULT 1,
  status      text NOT NULL DEFAULT 'queued',      -- queued|running|succeeded|failed|cancelled|timed_out
  input       jsonb NOT NULL,                      -- resolved prompt context refs
  output_summary text,
  error       text,
  provider    text, model text,
  tokens_in   int DEFAULT 0, tokens_out int DEFAULT 0,
  cost_usd    numeric(10,4) DEFAULT 0,
  started_at  timestamptz, finished_at timestamptz
);
CREATE INDEX idx_runs_task ON runs(task_id, attempt DESC);
CREATE INDEX idx_runs_status ON runs(status) WHERE status IN ('queued','running');

CREATE TABLE artifacts (
  id        uuid PRIMARY KEY,
  run_id    uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind      text NOT NULL,             -- code_patch|file|test_report|doc|diagram|build_log
  path      text,                      -- repo-relative path if applicable
  storage_ref text NOT NULL,           -- object-store/filesystem key
  sha256    text NOT NULL,
  size_bytes bigint NOT NULL
);
CREATE INDEX idx_artifacts_project_kind ON artifacts(project_id, kind);

-- communication ----------------------------------------------------------------
CREATE TABLE messages (
  id         uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  thread_id  uuid NOT NULL,                        -- UUIDv7; groups a conversation
  kind       text NOT NULL,   -- task_assignment|progress_report|review_request|architecture_discussion|bug_report|decision_log|meeting_summary|conflict_resolution|human
  sender_agent_id uuid REFERENCES agents(id),
  sender_user_id  uuid REFERENCES users(id),
  body       jsonb NOT NULL,                       -- structured per-kind schema (Vol 2 §protocol)
  task_id    uuid REFERENCES tasks(id),
  CHECK (sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL)
);
CREATE INDEX idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX idx_messages_project_kind ON messages(project_id, kind, created_at DESC);

CREATE TABLE decisions (
  id          uuid PRIMARY KEY,
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title       text NOT NULL,
  context     text NOT NULL,
  decision    text NOT NULL,
  alternatives text NOT NULL,
  consequences text NOT NULL,
  status      text NOT NULL DEFAULT 'accepted',    -- proposed|accepted|superseded
  decided_by_agent_id uuid REFERENCES agents(id),
  supersedes_id uuid REFERENCES decisions(id)
);
CREATE INDEX idx_decisions_project ON decisions(project_id, created_at DESC);

-- events (outbox) ----------------------------------------------------------------
CREATE TABLE outbox (
  id uuid PRIMARY KEY, topic text NOT NULL, payload jsonb NOT NULL,
  published_at timestamptz NULL
);
CREATE INDEX idx_outbox_unpublished ON outbox(created_at) WHERE published_at IS NULL;
```

Notes: statuses are `text` + application enums (Pydantic) rather than Postgres enums, so adding states never needs `ALTER TYPE`; `citext` extension required; `memory_items` and plugin tables follow the same conventions and are detailed in Volumes 5 and 7 respectively, but are owned by their modules here.

**TITAN-BCK-009 (MUST)** — Cycle prevention: inserting into `task_dependencies` SHALL be validated by a recursive-CTE check inside the same transaction, rejecting cycles with `409`.
*Acceptance:* attempting A→B→C→A returns the `invalid-dependency-cycle` problem type; property-based test with random DAGs passes.

---

## 7. Redis Usage

Redis 7 plays three distinct roles, with distinct key namespaces:

| Role | Mechanism | Keys / streams |
|---|---|---|
| Cache | `GET/SET` + TTL, `SCAN`-free invalidation via key versioning | `cache:{entity}:{id}:v{n}` |
| Queues | **Redis Streams** + consumer groups | `stream:tasks.ready`, `stream:runs.execute`, `stream:runs.results`, `stream:events.domain` |
| Pub/Sub | fire-and-forget fan-out to WS gateways | `ws.project.{id}`, `ws.agent.{id}` |
| Coordination | `SET NX PX` locks, rate-limit counters, idempotency keys | `lock:task:{id}`, `rl:{sub}:{route}`, `idem:{key}` |

### 7.1 ADR-BCK-02 — Redis Streams for the MVP queue, NATS JetStream for scale-out

- **Context.** Agent orchestration needs at-least-once delivery, consumer groups, replay for debugging, and pending-message recovery — on a laptop, for free, with minimal moving parts. Redis is already required for cache and pub/sub.
- **Decision.** Use Redis Streams with consumer groups as the only queue at MVP. `XADD` with `MAXLEN ~ 100000`; consumers `XREADGROUP` with explicit `XACK`; a reaper task runs `XAUTOCLAIM` (min-idle 60 s) to recover work from dead consumers; poison messages move to `stream:{name}.dlq` after 3 delivery attempts (tracked via `XPENDING` delivery counter).
- **Alternatives considered.** NATS JetStream (better multi-tenancy, true durability semantics, subject hierarchies — but a second broker locally); Kafka (overkill, heavy); RabbitMQ (no cheap replay); Postgres `SKIP LOCKED` queue (viable, but couples queue churn to WAL and vacuum).
- **Trade-offs.** Streams durability is bounded by Redis persistence (we enable AOF `everysec` locally); the outbox pattern (TITAN-BCK-005) covers the loss window because Postgres remains the source of truth and the publisher re-drains unpublished rows. **Scale-out path:** the `events` module hides the broker behind a `QueueBus` protocol (`publish`, `consume`, `ack`, `nack`); a `QUEUE_BACKEND=nats` setting swaps in the JetStream implementation with identical message envelopes — no domain code changes.

**TITAN-BCK-010 (MUST)** — Every stream message SHALL carry the envelope `{event_id (uuid7), topic, occurred_at, actor, payload, trace_id}` and consumers SHALL be idempotent keyed on `event_id` (dedup set `seen:{consumer_group}` with 24 h TTL).
*Acceptance:* replaying an entire stream into a fresh consumer group produces no duplicate side effects in Postgres.

**TITAN-BCK-011 (SHOULD)** — DLQ depth SHALL be exported as the Prometheus gauge `titan_stream_dlq_depth{stream=…}` and alerts fire at >0 for 10 min.
*Acceptance:* injecting a poison message raises the gauge within one scrape interval.

---

## 8. Task Orchestration Engine

The orchestrator turns the PM/Project-Manager agents' plans (Vol 2, Vol 6) into scheduled, parallel, reviewable execution.

### 8.1 Task state machine

```
                 ┌──────────┐
   create ─────▶ │ backlog  │
                 └────┬─────┘  deps met + prioritized
                      ▼
                 ┌──────────┐   assign (agent match)   ┌──────────┐
                 │  ready   │ ───────────────────────▶ │ assigned │
                 └──────────┘                          └────┬─────┘
                      ▲                                     ▼ run started
                      │ reopen                         ┌────────────┐
                 ┌────┴─────┐        submit_review     │in_progress │
        ┌──────▶ │   done   │ ◀──┐  ┌──────────────────┴────────────┘
        │        └──────────┘   ok  ▼
        │                      ┌──────────┐  request_changes  ┌───────────────────┐
   approve (reviewer)          │in_review │ ────────────────▶ │ changes_requested │
        └──────────────────────┴──────────┘ ◀──────────────── └───────────────────┘
                                                resubmit
   any non-terminal state ── cancel ──▶ cancelled
```

**TITAN-BCK-012 (MUST)** — Transitions SHALL be executed only through `POST /tasks/{id}:transition`, enforced server-side by a transition table `{(from_status, action) → to_status, required_permission, required_actor_role}`; each transition writes a `messages` row (kind matching the action) and an outbox event `task.{action}`.
*Acceptance:* exhaustive unit tests cover every (state, action) pair; illegal pairs return 409.

**TITAN-BCK-013 (MUST)** — *Create/estimate/prioritize.* Tasks are created by agents (or humans) with `title`, `spec`, `priority` (1–5), and `estimate_points` (Fibonacci 1–13, set by the assignee-role agent during grooming). The scheduler orders the `ready` pool by `(priority ASC, blocked_dependents_count DESC, created_at ASC)` — critical work first, unblockers next, FIFO ties.
*Acceptance:* given a seeded fixture project, the scheduler's dequeue order matches the documented sort in an integration test.

**TITAN-BCK-014 (MUST)** — *Dependencies.* A task enters `ready` only when all `depends_on` tasks are `done`. Completing a task SHALL re-evaluate its dependents in the same transaction and `XADD` newly-ready tasks to `stream:tasks.ready`.
*Acceptance:* completing the last blocker makes the dependent visible to a worker within 1 s locally.

**TITAN-BCK-015 (MUST)** — *Assignment.* The scheduler matches `ready` tasks to agents by role capability (task labels ↔ agent `role`), current load (max 1 concurrent run per agent at MVP; `config.max_parallel` later), and acquires `lock:task:{id}` (`SET NX PX 30000`, heartbeat-renewed) before transitioning to `assigned`.
*Acceptance:* two workers racing for one task result in exactly one `assigned` transition (verified by a 100-iteration race test).

**TITAN-BCK-016 (MUST)** — *Review & reopen.* Every task SHALL have a reviewer distinct from its assignee (Code Reviewer agent by default). `approve` → `done`; `request_changes` → `changes_requested` with a structured review message; `reopen` (from `done`) requires `task:reopen` permission and creates a new run attempt with `attempt = max+1`.
*Acceptance:* a task cannot reach `done` without a review-approval message row from a distinct agent id.

**TITAN-BCK-017 (MUST)** — *Parallel execution.* Independent `ready` tasks SHALL execute concurrently across worker replicas and across distinct agents within one worker (asyncio task group, bounded by `WORKER_CONCURRENCY`, default 4).
*Acceptance:* with 2 workers × concurrency 4, eight independent tasks show overlapping `started_at`/`finished_at` windows.

**TITAN-BCK-018 (SHOULD)** — *Milestones & sprints.* Sprints are planning containers (Vol 6 owns ceremony logic): the orchestrator SHALL only auto-schedule `ready` tasks belonging to the **active** sprint (or sprintless hotfix tasks with priority 1), and a milestone auto-closes when all its tasks are `done`.
*Acceptance:* moving a task out of the active sprint removes it from the dequeue pool without changing its status.

### 8.2 Run lifecycle

A run is one agent attempt at a task: worker pops from `stream:runs.execute`, sets run `running`, resolves context (task spec + memory search + relevant artifacts), streams LLM output (deltas → `ws.project.{id}` pub/sub + append to artifact buffer), executes tools within the plugin sandbox (Vol 7), then writes artifacts, token/cost accounting, and a result to `stream:runs.results`, which the orchestrator consumes to drive the state machine (success → `submit_review`). Timeouts: soft 15 min (configurable) → `timed_out`, retriable twice with exponential backoff before DLQ.

---

## 9. API Gateway, Authentication & RBAC

### 9.1 Gateway concerns

At MVP the "gateway" is a FastAPI middleware stack inside `titan-core` (no separate gateway process to keep the laptop footprint small); in production, Kubernetes ingress (NGINX/Traefik) fronts it for TLS termination and coarse limits (Vol 9). Middleware order (outermost first): request-ID injection (`X-Request-ID`, UUIDv7, also the OTel trace correlation attribute) → CORS (allowlist, `localhost:3000` in dev) → authentication → RBAC → rate limiting → idempotency replay → body-size cap (2 MiB JSON; artifacts upload via presigned object-storage URLs, never through the API) → compression → router.

**TITAN-BCK-019 (MUST)** — Security headers (`X-Content-Type-Options: nosniff`, `Referrer-Policy`, HSTS in prod) SHALL be set by middleware; OpenAPI docs (`/docs`) require auth outside dev mode.
*Acceptance:* header assertions in the API test suite; `/docs` returns 401 when `TITAN_ENV=production` without a token.

### 9.2 Authentication

**TITAN-BCK-020 (MUST)** — AuthN SHALL be OAuth2/OIDC (authorization-code + PKCE) against a pluggable IdP — **Keycloak in the local Compose stack** (free, self-hosted), any OIDC provider (Google/GitHub/Auth0) via configuration — plus a `local` password mode for laptop-only use. TITAN issues its **own** JWTs after IdP exchange: RS256 access tokens (15 min TTL; claims `sub`, `email`, `roles`, `scope`, `jti`) and rotating refresh tokens (7 days, revocable via `jti` denylist in Redis).
*Acceptance:* full login round-trip against dockerized Keycloak in the integration suite; a revoked refresh token is rejected in < 1 s.

Agents and CI use **API keys** (`POST /auth/api-keys`, hash-at-rest with SHA-256, prefix-identifiable `titan_ak_…`), exchanged internally for short-lived service JWTs carrying an `agent_id` claim so audit trails always resolve to a principal.

### 9.3 RBAC model

Roles are permission bundles stored in `roles.permissions`; assignments in `user_roles` are org-wide (`project_id NULL`) or per-project. Permission strings are `resource:action`.

| Role | Key permissions |
|---|---|
| `owner` | everything, incl. `org:manage`, `plugin:install`, role assignment |
| `admin` | all project CRUD, `agent:configure`, `task:reopen`, `run:cancel` |
| `maintainer` | `project:write`, `task:*` except reopen, `decision:write` |
| `operator` | `task:transition`, `run:create`, `message:write` |
| `viewer` | `*:read` only |

**TITAN-BCK-021 (MUST)** — Every route SHALL declare its required permission via a dependency (`Requires("task:transition")`); checks resolve role → permissions with a per-request cache; agents' service JWTs map to the `operator` bundle scoped to their project.
*Acceptance:* a route without a `Requires` dependency fails a CI AST lint; permission-matrix tests cover all roles × representative endpoints.

---

## 10. Rate Limiting

**TITAN-BCK-022 (MUST)** — Rate limiting SHALL be enforced in Redis using a token-bucket Lua script (atomic `EVALSHA`), keyed per principal and route class, returning `429` with `Retry-After` and `X-RateLimit-{Limit,Remaining,Reset}` headers.
*Acceptance:* k6 burst test shows hard cutoff at configured limits; headers present on both 2xx and 429.

| Route class | Default (per principal) | Notes |
|---|---|---|
| Read (`GET`) | 300/min | burst 60 |
| Write (POST/PATCH) | 60/min | burst 20 |
| `:transition`, run creation | 30/min | protects orchestrator |
| Auth endpoints | 10/min per IP | brute-force guard |
| WS connections | 5 concurrent per user | connect-time check |
| LLM-triggering ops | project-level budget: `llm_profile.max_runs_per_hour` | cost control; `sys.notice` WS warning at 80 % |

Internal agent traffic bypasses per-route limits but is governed by the LLM budget and `WORKER_CONCURRENCY` — the platform must never rate-limit itself into deadlock.

---

## 11. Caching Strategy

Postgres is always authoritative; caches are read-through and invalidated by version bump (no wildcard deletes).

| Layer | What | TTL | Invalidation |
|---|---|---|---|
| Redis `cache:project:{id}:v{n}` | project DTO + board aggregates | 5 min | version key bump on write (outbox consumer) |
| Redis `cache:agents:{project}` | agent registry list | 60 s | bump on agent update |
| Redis `idem:{key}` | idempotency response replay | 24 h | TTL only |
| In-process (per replica) | JWKS keys, role→permission map, settings | 5 min | TTL only (small, safe to be stale) |
| HTTP | `ETag` on all GETs (from `version`) | — | client revalidates; `304` saves payloads |

**TITAN-BCK-023 (MUST)** — Hot read endpoints (`GET /projects/{id}`, `/board`, `/agents`) SHALL be served read-through from Redis; cache hit ratio exported as `titan_cache_hits_total` / `titan_cache_misses_total`.
*Acceptance:* repeated board fetches hit Redis (observed via metric delta); a task transition changes the board response within one poll cycle (< 1 s locally).

Deliberately **not** cached: task lists (change constantly; cursor pagination is cheap with the `(project_id,status,priority)` index), messages, runs.

---

## 12. Logging & Observability Hooks

**TITAN-BCK-024 (MUST)** — All logs SHALL be structured JSON via `structlog` with mandatory fields `ts, level, event, request_id, trace_id, span_id, module, principal, project_id` (where applicable); stdout only (collected by Loki via promtail/docker driver); no secrets, tokens, or full LLM prompts at INFO (prompt/response bodies go to run artifacts, DEBUG-gated).
*Acceptance:* log-schema test parses 100 % of lines emitted during the integration suite; a secret-pattern scanner over captured logs finds zero hits.

**TITAN-BCK-025 (MUST)** — OpenTelemetry SHALL instrument FastAPI, SQLAlchemy, redis-py, httpx, and the LLM gateway (spans: `llm.call` with attributes `provider, model, tokens_in, tokens_out, cost_usd`; `queue.consume` with `stream, group, delivery_count`), exported OTLP to the local collector; traces span API → outbox → stream → worker → LLM via propagated `trace_id` in the message envelope (TITAN-BCK-010).
*Acceptance:* one task's life (create → run → review) renders as a single connected trace in Grafana Tempo/Jaeger locally.

Prometheus metrics (via `/metrics`): RED metrics per route (`titan_http_request_duration_seconds` histogram), `titan_stream_depth`, `titan_stream_dlq_depth`, `titan_runs_active`, `titan_llm_tokens_total{provider,model,direction}`, `titan_llm_cost_usd_total`, `titan_ws_connections`. Health endpoints: `/healthz` (liveness, no deps), `/readyz` (checks Postgres, Redis, Qdrant with 500 ms budget). Dashboards and alert rules ship in `infra/grafana/` (Vol 9 extends for production).

---

## 13. Local Development: Docker Compose & Repository Layout

### 13.1 Compose topology

**TITAN-BCK-026 (MUST)** — `docker compose up` from the repo root SHALL start the full platform on one laptop with no paid dependencies.
*Acceptance:* fresh clone → `cp .env.example .env` → `docker compose up` → UI at `localhost:3000` creates a project end-to-end against Ollama.

```
┌─────────────────────────── docker-compose.yml ───────────────────────────┐
│                                                                           │
│  web (Next.js 15)            :3000  ──▶ api                               │
│  api (titan-core, uvicorn)   :8000  ──▶ postgres, redis, qdrant, keycloak │
│  worker (titan-worker) x2           ──▶ postgres, redis, qdrant, ollama   │
│  postgres:16-alpine          :5432   vol: pgdata                          │
│  redis:7-alpine (AOF on)     :6379   vol: redisdata                       │
│  qdrant/qdrant               :6333   vol: qdrantdata                      │
│  keycloak (dev-mode)         :8081   realm import: infra/keycloak/        │
│  ollama/ollama               :11434  vol: ollamamodels   [profile: local-llm] │
│  otel-collector              :4317                                        │
│  prometheus                  :9090   grafana :3001   loki :3100           │
│                                      [profile: observability]             │
└───────────────────────────────────────────────────────────────────────────┘
```

Compose **profiles** keep the default footprint small: core profile = web/api/worker/postgres/redis/qdrant/keycloak (~2.5 GB RAM); `--profile observability` and `--profile local-llm` are opt-in. All service config flows through environment variables with a checked-in `.env.example`; switching LLM provider is exactly one variable set (`LLM_PROVIDER=anthropic|openai|google|ollama` plus the provider key), per the canonical configuration-only rule.

### 13.2 Repository layout (monorepo)

```
titan/
├── docker-compose.yml
├── .env.example
├── Makefile                      # make dev / test / lint / migrate / seed
├── apps/
│   ├── web/                      # Next.js 15 + TS + Tailwind + shadcn/ui (Vol 4)
│   └── api/                      # Python 3.12 backend (this volume)
│       ├── pyproject.toml        # uv-managed; ruff, mypy --strict, pytest
│       ├── alembic/              # migrations (hand-written)
│       └── src/titan/
│           ├── main.py           # FastAPI app factory, middleware stack
│           ├── worker.py         # stream-consumer entrypoint
│           ├── settings.py       # pydantic-settings, env-driven
│           ├── gateway/          # authn/z deps, rate limit, idempotency, errors
│           ├── identity/         # routers/ service.py repo.py models.py schemas.py
│           ├── projects/         #   (same internal layout per module)
│           ├── orchestrator/     #   + state_machine.py scheduler.py
│           ├── agents/
│           ├── runs/
│           ├── messaging/
│           ├── memory/           # façade; internals per Vol 5
│           ├── plugins/          # registry; sandbox per Vol 7
│           ├── llm/              # provider gateway: anthropic|openai|google|ollama
│           ├── events/           # QueueBus, outbox publisher, WS fan-out
│           └── observability/    # otel.py logging.py metrics.py
├── packages/
│   └── shared-types/             # OpenAPI-generated TS client for web
├── infra/
│   ├── keycloak/realm.json
│   ├── grafana/  prometheus/  loki/  otel/
│   └── k8s/                      # production manifests (Vol 9)
└── docs/                         # this specification series
```

**TITAN-BCK-027 (SHOULD)** — The TypeScript API client in `packages/shared-types` SHALL be generated from the FastAPI OpenAPI spec in CI, failing the build on uncommitted drift, so frontend and backend contracts never diverge silently.
*Acceptance:* changing a Pydantic schema without regenerating the client fails CI.

---

## 14. Risks & Open Questions

| # | Risk / question | Impact | Mitigation / owner |
|---|---|---|---|
| R1 | Redis Streams durability gap (AOF `everysec`) could lose ≤1 s of queue messages on crash | Lost work signals | Outbox re-drain (TITAN-BCK-005) makes Postgres authoritative; accepted at MVP; JetStream at scale-out |
| R2 | Single shared Postgres becomes a hotspot with many concurrent runs | Latency, lock contention | Owning-module schema discipline enables per-module DB split; runs/artifacts extract first |
| R3 | LLM streaming over WS at high fan-out may saturate one API replica | UI lag | Pub/sub fan-out already decouples producers; add dedicated WS gateway replicas behind sticky LB when needed |
| R4 | Optimistic locking (`version` + `If-Match`) may frustrate agents retrying stale writes | Retry storms | Transitions are the only contended write path and are serialized via `lock:task:{id}`; ETags mainly protect human edits |
| R5 | Keycloak adds ~700 MB RAM to the core profile | Laptop footprint | `AUTH_MODE=local` (password + JWT, no IdP) documented for constrained machines; same JWT code path |
| Q1 | Should run artifacts live in local MinIO at MVP or plain bind-mounted filesystem? | Storage abstraction | Current spec: filesystem behind a `Storage` protocol; decide before beta whether MinIO joins a Compose profile (Vol 9 input needed) |
| Q2 | Per-agent token budgets vs per-project budgets — which is primary for cost control? | Cost governance | Volume 10 metrics work will inform; both counters are already recorded on `runs` |
| Q3 | Multi-tenancy (multiple orgs per deployment) is out of MVP scope — confirm in Volume 1 v1.1 | Schema (org_id columns) | If confirmed in, add `org_id` before GA to avoid painful backfill |

---

## 15. Cross-References

- **Volume 1 — Foundation**: canonical stack rationale, non-functional requirements this volume implements.
- **Volume 2 — Multi-Agent Intelligence**: agent roles bound to `agents.role`, structured message `body` schemas per `messages.kind`.
- **Volume 4 — Frontend & Dashboard**: consumes the REST/WS contracts in §4–§5 (board, live activity, logs).
- **Volume 5 — AI Brain (Memory, Knowledge & RAG)**: internals behind the `memory` module façade and Qdrant collections.
- **Volume 6 — Engineering Workflow**: sprint/review ceremonies that drive the orchestration state machine in §8.
- **Volume 7 — Integrations & Tool Ecosystem**: plugin registry tables, sandboxing, MCP; LLM gateway providers.
- **Volume 8 — Security**: threat model, secrets management, audit depth beyond §9–§10 baselines.
- **Volume 9 — Production & Deployment**: Kubernetes topology, scaling the extraction path, backup/DR for Postgres/Redis/Qdrant.
- **Volume 10 — Autonomous Evolution**: run metrics (`tokens`, `cost_usd`, attempts) feeding agent self-evaluation.

*End of Volume 3 — TITAN-VOL-03, Draft v1.0.*
