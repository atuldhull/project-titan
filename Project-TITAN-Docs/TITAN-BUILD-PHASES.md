# PROJECT TITAN — Phased Build Plan (86 Phases)

| | |
|---|---|
| **Doc ID** | TITAN-BUILD-PHASES v1.0 |
| **Date** | 2026-07-04 |
| **Supersedes** | `BUILD-PROMPTS.md` (the 10-prompt plan) |
| **Source** | Full analysis of Volumes 1–10; every phase prompt cites the exact spec sections it implements |

---

## Why 86 phases instead of 10 prompts

The original `BUILD-PROMPTS.md` compressed each specification volume into a single giant prompt. Measured against the volumes' own component inventories, most of those prompts pack 5–10 focused work sessions into one instruction — too much to build, test, and verify in one go, and far too much to debug when something breaks.

This plan divides the same work into phases sized for **one focused Claude Code session each** (size S = a few hours, M = a full session). Every phase:

- names the exact spec volume sections to read (and nothing more, to keep context focused);
- lists concrete deliverables (tables, endpoints, screens, tools by name);
- declares its dependencies, so nothing is built before what it needs;
- ends with a **Definition of done** a human can verify in under 15 minutes.

The plan was cross-checked for spec coverage, dependency ordering, and honest sizing. Phases 79–86 (Kubernetes scale-out, extra connectors, the evolution layer) are **optional post-MVP** work — TITAN is fully usable after Phase 78.

### How the old 10 prompts map to milestones

| Old prompt | Now covered by |
|---|---|
| Prompt 1 — Scaffold & Foundation | M1 (Phases 1–5) |
| Prompt 2 — Database Schema & Core API | M2 (Phases 6–14, 24) |
| Prompt 4 — LLM Gateway & Tool Layer | M2 (Phases 15–23) |
| Prompt 3 — Agent Framework & Message Protocol | M3 (Phases 26–32), plus the Phase-25 walking skeleton |
| Prompt 5 — Memory, Knowledge & RAG | M4 (Phases 33–40) |
| Prompt 6 — Autonomous Workflow Engine | M5 (Phases 41–56) |
| Prompt 7 — Dashboard Frontend | M6 (Phases 57–66) + shell/walking-skeleton pieces in M1/M2 |
| Prompt 8 — Security Hardening | M7 security phases (67–74) + security built-in earlier (sandbox, secrets, RBAC) |
| Prompt 9 — CI/CD, Observability & Production | M7 ops phases (75–78) + CI/observability front-loaded in M1 |
| Prompt 10 — Evolution Features | M8 (Phases 79–86, optional) |

---

## How to use (step by step)

1. Create an empty folder for the project, e.g. `D:\titan`.
2. Copy this `Project-TITAN-Docs` folder inside it, so the specs live at `D:\titan\Project-TITAN-Docs\`.
3. Open Claude Code in `D:\titan`.
4. Paste the **Phase 1** prompt. Let it finish completely.
5. **Run the Definition of done checks yourself.** Every phase is designed so this takes under 15 minutes. If it passes, commit (`git commit`) so each phase is a restore point.
6. Only then paste the next phase, in numeric order. Never skip a phase; never run two at once.
7. If a phase fails or looks wrong, do **not** move on — use the Fix-It prompt at the bottom of this file.
8. Stopping at any completed milestone leaves you with a working, demonstrable system (see the milestone table).

---

## Milestones

| Milestone | Phases | What is demonstrably working at the end |
|---|---|---|
| **M1 — Running skeleton** | 1–5 | Compose stack healthy, CI gates red/green, traces in Tempo, dashboard shell routes 13 surfaces |
| **M2 — Data spine & walking skeleton** | 6–25 | Brief submitted in browser yields CEO-generated stories streamed live, traced, cost-metered |
| **M3 — Agents talk** | 26–32 | Multi-agent exchange with failure ladder, conflict resolution, veto; crash-safe replay |
| **M4 — The brain** | 33–40 | Cited Memory Packets; second agent recalls first agent's learning <5s; eval gate in CI |
| **M5 — Autonomous delivery** | 41–56 | Mock-LLM brief runs all 12 handoffs; full 16-agent todo-app completes offline on Ollama |
| **M6 — Dashboard complete** | 57–66 | All 13 screens live on real data, realtime, responsive, axe-clean |
| **M7 — Hardened & production-ready** | 67–78 | Red-team corpus blocked, lockdown drill <=60s, PITR restore verified, readiness checklist signed |
| **M8 — Scale-out & evolution (optional)** | 79–86 | K8s autoscaling under load; human-gated evolution proposals with bench gates |

---

## Phase index

| # | Phase | Milestone | Size | Depends on |
|---|---|---|---|---|
| 1 | Monorepo scaffold & toolchain | M1 | S | — |
| 2 | Compose core stack + FastAPI skeleton + health | M1 | M | 1 |
| 3 | CI quality gates + Pydantic→TypeScript bridge | M1 | M | 2 |
| 4 | Observability foundation in Compose | M1 | M | 2 |
| 5 | Dashboard shell (13-surface nav + design system) | M1 | M | 1 |
| 6 | PostgreSQL schema, migrations & cycle check | M2 | M | 2 |
| 7 | REST conventions framework + gateway middleware | M2 | M | 6 |
| 8 | Identity: local auth, JWTs, API keys, service tokens | M2 | M | 7 |
| 9 | RBAC engine + route protection + ownership | M2 | M | 8 |
| 10 | Events infra: QueueBus, Redis Streams & transactional outbox | M2 | M | 6 |
| 11 | Domain modules I: projects, milestones, sprints, tasks | M2 | M | 7, 9 |
| 12 | Domain modules II: agents, messages, decisions, runs, artifacts, notifications | M2 | M | 11 |
| 13 | Task transitions, scheduler & race-safe assignment | M2 | M | 10, 12 |
| 14 | Worker process & run lifecycle | M2 | S | 13 |
| 15 | LLM Gateway core: Anthropic + Ollama + embeddings | M2 | M | 1, 2 |
| 16 | OpenAI + Google adapters + conformance matrix | M2 | S | 15 |
| 17 | Gateway resilience, cost accounting & budgets | M2 | M | 16 |
| 18 | Tool Registry + Tool Bus pipeline | M2 | M | 9, 15 |
| 19 | Risk classes, approval gate & tool fuzz campaign | M2 | S | 18 |
| 20 | Workspace sandbox + fs tools | M2 | M | 19 |
| 21 | git, terminal & code tools | M2 | M | 20 |
| 22 | docker & browser tools | M2 | M | 20 |
| 23 | web tools + registry mirror + egress proxy | M2 | M | 20 |
| 24 | WebSocket realtime backend | M2 | M | 9, 10, 12 |
| 25 | Walking skeleton: CEO brief → stories, live in UI | M2 | M | 4, 5, 11, 15, 17, 24 |
| 26 | AgentDefinition schema, 16-agent roster & registry | M3 | M | 6 |
| 27 | Message schemas, persistence, DecisionLog & delivery | M3 | M | 6, 10 |
| 28 | Prompt composition engine + 16 templates + model policy | M3 | M | 26 |
| 29 | Agent runtime turn loop + tool-grant enforcement | M3 | M | 18, 27, 28 |
| 30 | Failure-recovery ladder + KPI emission | M3 | M | 4, 29 |
| 31 | Conflict-resolution state machine + security veto | M3 | M | 29, 30 |
| 32 | Watchdog + single-owner constraint | M3 | S | 31 |
| 33 | Brain schema, Memory Events & ledger | M4 | M | 6 |
| 34 | Content-aware chunkers | M4 | M | 33 |
| 35 | Qdrant collections + embedding ingestion pipeline | M4 | M | 4, 10, 15, 33 |
| 36 | Hybrid retrieval + reranker + Memory Packet | M4 | M | 34, 35 |
| 37 | Memory eval harness + performance gate | M4 | M | 36 |
| 38 | Working memory, context budgets & runtime memory wiring | M4 | M | 29, 36 |
| 39 | Knowledge base + knowledge graph | M4 | M | 36 |
| 40 | Decay, curation, reflections & lessons | M4 | M | 38, 39 |
| 41 | Pipeline & task state machines | M5 | M | 14, 17, 24, 29 |
| 42 | Handoff executor, gate ledger & 12 artifact schemas | M5 | M | 41 |
| 43 | Gates G1–G5 + constitution document + CEO clarification engine | M5 | M | 42 |
| 44 | Self-improvement loop engine + reviewer isolation | M5 | M | 31, 43 |
| 45 | Constitution CI enforcement kit | M5 | M | 21, 43 |
| 46 | Generated-workspace templates: errors, logging, test pyramid | M5 | S | 45 |
| 47 | Reviewer R1–R11 rules + fixtures + doc/quickstart checkers | M5 | M | 22, 44, 45, 46 |
| 48 | Git workspaces + local Compose deploy | M5 | M | 21, 22, 23 |
| 49 | Gates G6–G12 + traceability + debt ledger | M5 | M | 47, 48 |
| 50 | Sprint planner, ceremonies, milestones & blocker ladder | M5 | M | 43 |
| 51 | Derived artifacts + event replay + mock-LLM E2E | M5 | M | 40, 49, 50 |
| 52 | Plugin runtime: manifest, lifecycle, sandbox, webhooks, UI panels | M5 | M | 19, 20 |
| 53 | MCP client | M5 | M | 52 |
| 54 | GitHub, Slack & Discord connectors | M5 | M | 52 |
| 55 | Chaos suite + benchmark harness | M5 | M | 51, 54 |
| 56 | Full-roster offline acceptance run (AGT-007) | M5 | M | 55 |
| 57 | Frontend data layer, auth/BFF, notifications & WS client | M6 | M | 3, 5, 12, 24 |
| 58 | Overview, Projects & intake wizard screens | M6 | M | 25, 43, 51, 57 |
| 59 | Kanban board | M6 | M | 41, 57 |
| 60 | Graph engine, task graph & Architecture Viewer | M6 | M | 57 |
| 61 | Agent grid + Live Conversations | M6 | M | 57 |
| 62 | Logs Viewer + Loki history endpoint | M6 | M | 4, 57 |
| 63 | Deployment, Monitoring & Memory Explorer screens | M6 | M | 17, 40, 48, 57 |
| 64 | Plugin Manager + Settings screens | M6 | M | 17, 53, 57 |
| 65 | Command palette, shortcuts, responsive & a11y sweep | M6 | M | 58, 59, 60, 61, 62, 63, 64 |
| 66 | Frontend test & CI enforcement | M6 | S | 65 |
| 67 | Auth hardening: OIDC service, Argon2id, throttling, MFA | M7 | M | 8, 9, 57 |
| 68 | Secrets vault, scoped injection, encryption & signed envelopes | M7 | M | 17, 18, 20 |
| 69 | Redaction engine (three sinks) + secret-file exclusion | M7 | M | 4, 35, 68 |
| 70 | Input validation, rate limiting, SSRF & webhook hardening | M7 | M | 12, 23, 54 |
| 71 | Budget tier extension, loop detection & frontend CSP | M7 | M | 17, 57, 70 |
| 72 | Feature flags, kill switches & backpressure | M7 | M | 14, 70 |
| 73 | Audit chain + event-catalog instrumentation | M7 | M | 68 |
| 74 | CI security pipeline + dependency gate + SECURITY.md | M7 | M | 3, 23 |
| 75 | Incident lockdown + red-team harness + security check CLI | M7 | M | 72, 73, 74 |
| 76 | CI/CD release pipelines, signing & migration-compat gate | M7 | M | 3, 74 |
| 77 | Grafana dashboards-as-code + SLO alerting | M7 | M | 4, 17, 30 |
| 78 | Backups, restore, runbooks & production readiness | M7 | M | 76, 77 |
| 79 | K8s base, resource governance & HA *(optional)* | M8 | M | 78 |
| 80 | Autoscaling + blue/green deployment *(optional)* | M8 | M | 79 |
| 81 | DR kit, Terraform & NATS adapter *(optional)* | M8 | M | 80 |
| 82 | Extra connectors: GitLab, Jira, Linear, Figma *(optional)* | M8 | M | 54 |
| 83 | Evolution: artifact versioning, prompt registry & EP gate *(optional)* | M8 | M | 51, 78 |
| 84 | Scorecards, KPI rollup & degradation alerts *(optional)* | M8 | M | 83 |
| 85 | Reflections, retrospectives, failure taxonomy & RCR *(optional)* | M8 | M | 40, 83 |
| 86 | TITAN-Bench, A/B harness & debate protocol *(optional)* | M8 | M | 84, 85 |

---

# The Prompts

---

## M1 — Running skeleton

> **Milestone exit state:** Compose stack healthy, CI gates red/green, traces in Tempo, dashboard shell routes 13 surfaces

### Phase 1 — Monorepo scaffold & toolchain

**Milestone:** M1 — Running skeleton · **Size:** S · **Depends on:** nothing

```
You are starting PROJECT TITAN, a multi-agent autonomous software engineering
platform. First set up the workspace: create an empty project folder (e.g. D:\titan),
copy the Project-TITAN-Docs folder into it so the specs live at
./Project-TITAN-Docs/, and work from the project root. Treat ./Project-TITAN-Docs/
as the single source of truth for every decision.
Phases already completed: none — this is the first phase.

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
§10 (system architecture), §11–11.1 (canonical stack) and ADR-001/002/003 in §11.2;
Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §2.1
(topology, TITAN-BCK-001/002/004) and §13.2 (repository layout).

Build PHASE 1 — the monorepo scaffold and toolchain:
1. Create the ADR-001 monorepo layout exactly: apps/web (Next.js 15 App Router +
   strict TypeScript + Tailwind CSS + shadcn/ui), apps/api (Python 3.12 + FastAPI +
   SQLAlchemy 2.x async + Pydantic v2), packages/schemas (generated TS types land
   here in a later phase), infra/, docs/. Match the Volume 3 §13.2 tree: apps/api
   has pyproject.toml, alembic/, and src/titan/ with main.py, worker.py, settings.py
   entrypoint stubs that import cleanly.
2. Lay down the modular-monolith seams inside apps/api/src/titan/ as packages:
   gateway, identity, projects, orchestrator, agent_runtime, messaging, memory,
   plugins, events, observability. Per TITAN-BCK-004, each domain module exposes
   exactly one public interface (<module>/service.py with Pydantic v2 DTOs); routers,
   repositories, and models stay private to the module.
3. Toolchains, all strict from day one: ruff (check + format), mypy --strict, eslint,
   prettier, tsc --strict; Python dependencies managed by uv via pyproject.toml; a
   root Makefile with dev / test / lint / migrate / seed targets (migrate and seed
   may print "not yet wired" for now — the targets must exist and lint/test must
   really run).
4. import-linter contracts encoding the module boundaries of TITAN-BCK-001: no
   cross-module imports except via a module's public service.py; wire the check into
   `make lint`. Add a root README skeleton (what TITAN is, how to run lint/test,
   pointer to Project-TITAN-Docs).

This phase is scaffolding — no Docker, no database, no endpoints yet. No TODOs in
backend code; empty modules get a real __init__.py and service.py stub, not
placeholder comments.

Definition of done: ruff, mypy --strict, eslint, and tsc --strict all pass on the
skeleton; the import-linter boundary check exists, runs in `make lint`, and passes;
the repo tree matches ADR-001 and Volume 3 §13.2 (apps/web, apps/api, packages/
schemas, infra/, docs/, and all ten titan module packages present).
```

### Phase 2 — Compose core stack + FastAPI skeleton + health

**Milestone:** M1 — Running skeleton · **Size:** M · **Depends on:** 1

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 1 (monorepo layout with titan module seams, strict
toolchains, Makefile, import-linter).

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
§9.1 (MVP scope), TITAN-FND-060 (local-first NFR), and ADR-004/005/006/008 in §11.2;
Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §2.1
(TITAN-BCK-001/002/003), §12 (TITAN-BCK-024 logging, health endpoints), §13.1
(Compose topology, TITAN-BCK-026); Project-TITAN-Docs/09-Volume-9-Production-and-
Operations.md §3.3 (image build spec).

Build PHASE 2 — the local stack and the FastAPI skeleton:
1. docker-compose.yml at the repo root per Volume 3 §13.1: web (:3000), api
   (uvicorn titan.main:app, :8000), postgres:16-alpine (:5432, pgdata volume,
   citext extension created), redis:7-alpine with AOF on (:6379, redisdata volume),
   qdrant (:6333, qdrantdata volume); ollama (:11434, ollamamodels volume) only
   under --profile local-llm. Provide a checked-in .env.example (all config flows
   through env vars); total footprint must fit a 16 GB laptop. Add a --profile
   minimal path so constrained machines run only the core containers.
2. Five hardened Dockerfiles under infra/docker/ per Volume 9 §3.3: titan-frontend
   (Next.js standalone output on node:22-slim, non-root), titan-api,
   titan-orchestrator, titan-worker (all three from one shared python:3.12-slim
   base stage with uv-installed dependencies, non-root, read-only root FS), and
   titan-migrate (Alembic + migration scripts only). Buildable multi-arch via
   buildx (linux/amd64,linux/arm64).
3. Backend skeleton in apps/api: FastAPI app factory in titan/main.py,
   pydantic-settings in titan/settings.py driven entirely by env vars, async
   SQLAlchemy 2.0 engine (asyncpg) and redis.asyncio clients (TITAN-BCK-002 — no
   sync drivers in pyproject.toml), and structlog JSON logging to stdout with the
   TITAN-BCK-024 mandatory fields (ts, level, event, request_id, trace_id, span_id,
   module) plus X-Request-ID middleware issuing UUIDv7 request ids.
4. Alembic wired with an empty baseline revision, and the hand-written-migration
   policy from Volume 3 §13.2 documented in apps/api/alembic/README (no
   autogenerate-and-commit workflow).
5. Health endpoints per Volume 3 §12: /healthz (liveness, touches no dependencies)
   and /readyz (checks Postgres, Redis, and Qdrant within the 500 ms budget,
   returns 503 listing the failing dependency). Write pytest coverage for both,
   including the dependency-down case. No TODOs in backend code.

Extend the Phase-1 Makefile and module seams — do not introduce a second config or
entrypoint scheme.

Definition of done: fresh clone → cp .env.example .env → docker compose up reaches
healthy in under 15 minutes (TITAN-FND-060/TITAN-BCK-026); /readyz returns 200 with
all dependencies up and 503 when one is stopped, and tests prove both; alembic
upgrade head runs clean on the fresh database; every log line parses as JSON and
carries a request_id.
```

### Phase 3 — CI quality gates + Pydantic→TypeScript bridge

**Milestone:** M1 — Running skeleton · **Size:** M · **Depends on:** 2

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 1 (monorepo scaffold, strict toolchains, import-linter
boundaries), 2 (Compose stack, FastAPI app factory with /healthz and /readyz,
structlog JSON logging, Alembic baseline).

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
§12 (engineering principles), ADR-001/007 in §11.2, TITAN-FND-062 (secure defaults /
secret scanning) and TITAN-FND-064 (typing, lint, coverage gates);
Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md
TITAN-BCK-001 (import-linter acceptance), TITAN-BCK-002 (flake8-async acceptance),
TITAN-BCK-006 (API conventions), TITAN-BCK-027 (generated TS client drift gate).

Build PHASE 3 — CI quality gates and the schema bridge:
1. A GitHub Actions CI workflow (.github/workflows/ci.yml) following the Volume 9
   stage order lint → typecheck → tests: ruff check + ruff format --check, eslint +
   prettier --check, mypy --strict, tsc --noEmit under strict settings, pytest with
   coverage ≥ 80% on core backend code (fail below threshold), and gitleaks secret
   scanning (TITAN-FND-062). Every gate must actually fail the build, not warn.
2. The ADR-007 boundary rule: provider SDK imports (anthropic, openai,
   google-generativeai, ollama client libraries) are banned everywhere except the
   titan/llm gateway package — enforce via an import-linter forbidden contract or
   ruff banned-API rule, running in CI. Agents never import provider SDKs directly;
   this rule is the enforcement demanded by ADR-007.
3. The Pydantic/OpenAPI → TypeScript bridge (TITAN-BCK-027): a `make generate-types`
   step that emits the FastAPI OpenAPI spec and generates TypeScript types into
   packages/schemas, plus a CI drift check that regenerates and fails on any
   uncommitted diff. Prove it end-to-end on the Phase-2 /healthz and /readyz
   response schemas: change the Pydantic model without regenerating → CI fails.
4. Add the Phase-1 import-linter contracts and flake8-async (blocking-call
   detection per TITAN-BCK-002) to CI so boundary and async violations block merge.

Extend the Phase-1 lint config and Makefile targets — CI must call the same
commands developers run locally, not a parallel set.

Definition of done: CI goes red on each of these, proven by a temporary violating
commit or test fixture: a type error, a lint violation, a coverage drop below 80%,
a committed secret, a provider-SDK import outside titan/llm, and stale generated TS
types; the generated TypeScript compiles under tsc --strict; CI is green on the
clean tree.
```

### Phase 4 — Observability foundation in Compose

**Milestone:** M1 — Running skeleton · **Size:** M · **Depends on:** 2

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 1 (monorepo scaffold and toolchains), 2 (Compose core
stack, FastAPI app with /healthz and /readyz, structlog JSON logging with
request_id).

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
§9.1, TITAN-FND-061 (observability NFR), ADR-011 in §11.2;
Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §12
(TITAN-BCK-024/025, Prometheus metric names, health endpoints);
Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §6.1–6.4 (metrics,
dashboards-as-code, Loki labels, tracing) and TITAN-OPS-016.

Build PHASE 4 — the observability foundation:
1. OpenTelemetry SDK instrumentation of the Phase-2 app per TITAN-BCK-025: FastAPI,
   SQLAlchemy, redis-py, and httpx auto-instrumentation exporting OTLP to a local
   otel-collector (config in infra/otel/); inject trace_id/span_id into every
   structlog line so logs correlate with traces; implement it in the
   titan/observability module from Phase 1.
2. Compose --profile obs adding prometheus (:9090), grafana (:3001), tempo, loki
   (:3100), promtail, and otel-collector (:4317). Grafana is provisioned entirely
   as code in infra/grafana/ (no click-ops): Prometheus + Tempo + Loki datasources
   and a platform health dashboard (service up/down, RED panels per route,
   /readyz dependency status).
3. Trace-context plumbing: reserve and document the `traceparent` field in the
   Redis Streams message-envelope contract now (TITAN-OPS-016 — workers resume the
   context on consume in a later phase); add RED middleware emitting the
   titan_http_request_duration_seconds histogram per route and expose /metrics in
   the Prometheus format with the Volume 3 §12 metric names.
4. Ship structured logs to Loki via promtail with labels {namespace, service,
   level} only — low cardinality per Volume 9 §6.3; everything else stays in the
   JSON body. No secrets or prompt bodies in logs (TITAN-BCK-024).

Extend the Phase-2 docker-compose.yml and structlog configuration — do not build a
second compose file or a second logging setup. No TODOs in backend code.

Definition of done: one /readyz request is visible in all three systems — as a
trace in Tempo, in the request-duration metric in Prometheus, and as a JSON log
line carrying its trace_id in Loki; the Grafana health dashboard renders service
health from live data; docker compose up with --profile minimal starts none of the
observability containers.
```

### Phase 5 — Dashboard shell (13-surface nav + design system)

**Milestone:** M1 — Running skeleton · **Size:** M · **Depends on:** 1

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 1 (monorepo scaffold with apps/web Next.js 15 + strict
TypeScript + Tailwind + shadcn/ui, eslint/prettier/tsc toolchain).

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
TITAN-FND-022 (dashboard requirement) and ADR-003 in §11.2;
Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §2 (route map, app shell,
TITAN-FRT-006), §3 (design system, TITAN-FRT-010/011/012/013), §11 (folder
structure, TITAN-FRT-049), ADR-FRT-02 and ADR-FRT-06 in §12.

Build PHASE 5 — the dashboard shell and design system:
1. Design tokens as CSS custom properties in styles/globals.css with the exact
   Volume 4 §3.2 values for both themes (--background, --card, --foreground,
   --muted-foreground, --primary, --destructive, --success, --warning, --border,
   --ring; --radius: 8px). Dark is the default via the class strategy
   (<html class="dark">) with a blocking inline script applying the persisted
   choice before first paint (ADR-FRT-06 — no theme flash), a light-mode toggle
   that persists, Inter and JetBrains Mono self-hosted via next/font (no font CDN —
   offline requirement), and shadcn/ui primitives vendored into components/ui and
   treated as first-party code (ADR-FRT-02, TITAN-FRT-013).
2. The app shell per Volume 4 §2.3: 240 px sidebar collapsible to a 64 px icon
   rail with the state persisted in localStorage, and a 56 px top bar (project
   switcher, ⌘K trigger, connection-status pill, notification bell, user menu —
   visual stubs are fine). App Router routes for all 13 Volume 4 §2.2 surfaces
   using the §11 folder structure: /org, /projects, /projects/[pid] (+ board,
   graph, architecture, deployments), /agents, /conversations, /logs,
   /deployments, /monitoring, /memory, /plugins, /settings/* — plus (auth)/login.
   Page bodies may be stubs labelled "coming in later phase" (allowed ONLY for
   pages, never for backend code); routes, titles, and sidebar highlighting must
   be real, and navigational state URL-encoded per TITAN-FRT-006.
3. Lint enforcement: a regex-based ESLint rule rejecting raw hex/rgb color values
   in apps/web so components can only consume semantic tokens (TITAN-FRT-010), and
   ESLint import boundaries keeping feature code in its feature directory with
   cross-feature imports only via lib/ or packages/ (TITAN-FRT-049). Commit
   violating fixtures proving both rules fail.
4. Responsive shell: sidebar collapses to the icon rail / drawer pattern so every
   surface is usable at 375 px width (TITAN-FND-022 acceptance).

Definition of done: all 13 surfaces route and render inside the shell; the app is
usable at 375 px; Lighthouse accessibility score ≥ 90; an anonymous first load
paints dark with no theme flash and the light toggle persists across reload; the
token lint rule and import-boundary rule each fail on their violating fixture and
pass on the clean tree.
```

---

## M2 — Data spine & walking skeleton

> **Milestone exit state:** Brief submitted in browser yields CEO-generated stories streamed live, traced, cost-metered

### Phase 6 — PostgreSQL schema, migrations & cycle check

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 2

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 2 (Docker Compose core stack — PostgreSQL 16, Redis 7, Qdrant — plus the FastAPI app factory, pydantic-settings config, Alembic wiring, and the /api/v1/health endpoint).

Read fully:
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §6 (PostgreSQL Schema, TITAN-BCK-008/009) and §9.3 (RBAC roles matrix, for the seed data)
- Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §7 (Notifications System, TITAN-FRT-038 — the notifications table must support it)

Build PHASE 6 — the 15-table data spine:
1. SQLAlchemy models + hand-written Alembic migrations (single head; autogenerate is
   forbidden — hand-reviewed migrations only, per §6) creating exactly the schema in
   Volume 3 §6: users (citext email, UNIQUE(auth_provider, auth_subject)), roles
   (name + permissions jsonb), user_roles (project_id NULL = org-wide), projects
   (unique slug, status intake|planning|building|reviewing|released|archived, mode,
   llm_profile, created_by), milestones, sprints (UNIQUE(project_id, number)),
   agents (role, status, model_binding jsonb, config jsonb, current_task_id), tasks
   (status backlog…cancelled, priority 1–5, estimate_points, assignee/reviewer/
   created_by agent FKs, parent_id for epics), task_dependencies (composite PK,
   CHECK task_id <> depends_on_id), runs (attempt, input jsonb, provider/model,
   tokens_in/out, cost_usd numeric(10,4)), artifacts (kind, path, storage_ref,
   sha256, size_bytes), messages (thread_id, 9 kinds, body jsonb, CHECK that a
   sender agent or user exists), decisions (context/decision/alternatives/
   consequences, supersedes_id), and outbox (topic, payload, published_at NULL).
2. Add the 15th table, notifications, to satisfy TITAN-FRT-038: per-user persisted
   rows (user_id FK, category approval|failure|progress|activity|security, severity,
   title, body, deep-link, read_at NULL) with a partial index on (user_id) WHERE
   read_at IS NULL so the bell badge count survives reload cheaply.
3. Conventions on EVERY table (TITAN-BCK-008): created_at timestamptz NOT NULL
   DEFAULT now(), updated_at maintained by one reusable trigger function, version
   int NOT NULL DEFAULT 1. Statuses are text + Pydantic enums, never Postgres enums.
   Enable the citext extension in a migration. Create ALL specified indexes,
   including the partial ones: idx_tasks_assignee WHERE status IN
   ('assigned','in_progress'), idx_runs_status WHERE status IN ('queued','running'),
   idx_outbox_unpublished WHERE published_at IS NULL.
4. Cycle prevention (TITAN-BCK-009): inserting into task_dependencies runs a
   recursive-CTE reachability check inside the same transaction; a cycle raises a
   domain error that maps to HTTP 409 with problem type
   https://titan.dev/errors/invalid-dependency-cycle.
5. Seed script for local dev: the five §9.3 roles (owner, admin, maintainer,
   operator, viewer) with their exact permission bundles in roles.permissions, plus
   a default local admin user.

No TODOs in backend code; every migration must downgrade cleanly.

Definition of done: `alembic upgrade head` on an empty Postgres 16 produces exactly
these tables and indexes, and `pytest -m schema` asserts them; a test updates a row
and proves the trigger changed updated_at; a property-based test with random DAGs
plus the explicit A→B→C→A case proves cycle inserts are rejected.
```

### Phase 7 — REST conventions framework + gateway middleware

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 6

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 6 (15-table PostgreSQL schema with version columns, trigger-maintained updated_at, cycle check, seeded roles + local admin).

Read fully: Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §4.1 (API conventions, TITAN-BCK-006) and §9.1 (Gateway concerns, TITAN-BCK-019)

Build PHASE 7 — the shared REST machinery every later module will use:
1. Conventions framework under /api/v1 (TITAN-BCK-006): UUIDv7 ids everywhere;
   plural kebab-case resources; snake_case JSON bodies; cursor pagination
   (?limit=50&cursor=<opaque>) with lists enveloped as {"items":[…],"next_cursor":…}
   and single resources unenveloped; a global RFC 9457 problem+json exception
   handler emitting {type, title, status, detail, instance, errors[]}; RFC 3339 UTC
   timestamp serialization (2026-07-04T12:00:00Z).
2. Optimistic concurrency + idempotency: ETag computed from the Phase 6 row
   `version` column; mutating updates require If-Match and return 412 on mismatch;
   mutating POSTs accept an Idempotency-Key header replayed for 24 h from Redis
   (keys idem:{key} per §7).
3. Middleware stack in EXACTLY this order, outermost first (§9.1): X-Request-ID
   injection (UUIDv7, also the OTel trace correlation attribute) → CORS allowlist
   (localhost:3000 in dev) → authentication (named stub slot — Phase 8 fills it) →
   RBAC (named stub slot — Phase 9 fills it) → rate limiting (stub slot) →
   idempotency replay → 2 MiB JSON body-size cap → compression → router. Security
   headers per TITAN-BCK-019 (X-Content-Type-Options: nosniff, Referrer-Policy,
   HSTS in prod); /docs requires auth when TITAN_ENV=production.
4. Commit a Spectral ruleset encoding the §4.1 conventions table, runnable in CI
   against the generated OpenAPI spec.

Extend the FastAPI app factory from Phase 2 — do not build a parallel app. The
authn/RBAC/rate-limit stubs must be replaceable slots, not throwaway code.

Definition of done: a stale If-Match returns 412; a replayed Idempotency-Key
returns the cached response; all error responses are problem+json with
type/title/status/detail/instance; /docs returns 401 when TITAN_ENV=production;
spectral passes against the OpenAPI spec.
```

### Phase 8 — Identity: local auth, JWTs, API keys, service tokens

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 7

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 7 (REST conventions framework — UUIDv7, pagination, problem+json, ETag/If-Match, Idempotency-Key — and the ordered gateway middleware stack with named authn/RBAC/rate-limit stub slots).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md (TITAN-FND-029 and ADR-010 — local mode must work offline with no external IdP)
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §9.2 (Authentication, TITAN-BCK-020)
- Project-TITAN-Docs/08-Volume-8-Security.md §3.2 (TITAN-SEC-011 token requirements, TITAN-SEC-013 cookie constraint)

Build PHASE 8 — bootstrap identity that runs 100% offline:
1. AUTH_MODE=local: the Phase 6 seeded admin logs in with username/password against
   the built-in identity module — no Keycloak, no network. TITAN issues its OWN
   RS256 access JWTs, TTL ≤ 15 min, with claims sub, org, roles, scope, jti (plus
   iat/exp/aud/iss); verifiers reject alg=none and HS256, expired tokens, and wrong
   aud/iss (TITAN-SEC-011). Refresh tokens are opaque, 7-day, single-use, rotated
   on every use with FAMILY REUSE DETECTION: presenting an already-consumed refresh
   token revokes the entire family. Revoked jti values go to a Redis denylist
   checked per request so revocation takes effect in under 60 s.
2. Endpoints POST /auth/token, POST /auth/refresh, GET /auth/me — built on the
   Phase 7 conventions framework (failures are problem+json), and wire real token
   verification into the Phase 7 authn stub slot — do not add a second auth layer.
   Design token delivery so the future Next.js BFF can hold them in httpOnly
   cookies (TITAN-SEC-013) — never assume localStorage.
3. POST /auth/api-keys for agents and CI: keys are prefix-identifiable
   (titan_ak_…), stored ONLY as SHA-256 hashes, and exchanged internally for
   short-lived service JWTs carrying an agent_id claim so every audit trail
   resolves to a principal (§9.2).

No TODOs in backend code; tests for every endpoint (happy + failure paths).

Definition of done: the local login → refresh-rotation round-trip passes fully
offline; a reused refresh token revokes its whole family; a revoked token is
rejected in under 60 s (tested); API-key auth works end-to-end with only the hash
at rest.
```

### Phase 9 — RBAC engine + route protection + ownership

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 8

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 8 (local auth with RS256 JWTs + rotating refresh, jti denylist, API keys, service JWTs with agent_id claim, wired into the gateway authn slot).

Read fully:
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §9.3 (RBAC model, TITAN-BCK-021)
- Project-TITAN-Docs/08-Volume-8-Security.md §3.3–§4 (TITAN-SEC-012/018, the §4.2 permission matrix, and the object-level authorization rule)

Build PHASE 9 — server-side authorization on every route:
1. RBAC engine over the Phase 6 roles tables: the five §9.3 roles (owner, admin,
   maintainer, operator, viewer) as permission-string bundles (resource:action) in
   roles.permissions, org-wide or per-project via user_roles.project_id; plus the
   machine "agent" class — agents' service JWTs map to the operator bundle scoped
   to their project (TITAN-BCK-021). Resolve role → permissions once per request
   and cache it for that request.
2. A Requires("resource:action") FastAPI dependency as the SINGLE enforcement
   point, e.g. Requires("task:transition"); replace the Phase 7 RBAC stub slot with
   it — do not build a second authorization mechanism.
3. A CI AST lint that parses every registered route and FAILS the build if any
   route lacks a Requires dependency (TITAN-BCK-021 acceptance).
4. Object-level ownership checks on every ID reference: a resource outside the
   caller's org/project scope returns 404, never 403, so existence is not leaked
   (no IDOR).
5. Permission-matrix test suite: a matrix-as-code fixture exercising every
   registered route with every role and asserting allow/deny per §9.3.

Definition of done: the permission-matrix suite passes for every route × every
role; the AST lint demonstrably fails when a deliberately unprotected route is
added in a test fixture; an org-A token requesting an org-B resource gets 404.
```

### Phase 10 — Events infra: QueueBus, Redis Streams & transactional outbox

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 6

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 6 (15-table schema including the outbox table — topic, payload jsonb, published_at NULL — with the idx_outbox_unpublished partial index).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md (TITAN-FND-011 queue semantics, TITAN-FND-052 no-data-loss, ADR-005)
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §7 (Redis Usage, ADR-BCK-02, TITAN-BCK-005/010)

Build PHASE 10 — the event backbone every later module publishes through:
1. A QueueBus protocol in the events module — publish, consume, ack, nack — that is
   broker-agnostic (no Redis types in its signatures), so a QUEUE_BACKEND=nats
   JetStream implementation can swap in later with identical envelopes per
   ADR-BCK-02. Ship the Redis Streams implementation covering stream:tasks.ready,
   stream:runs.execute, stream:runs.results, and stream:events.domain.
2. Delivery mechanics per ADR-BCK-02: consumer groups with XREADGROUP + explicit
   XACK; producers XADD with MAXLEN ~100000; a reaper task running XAUTOCLAIM
   (min-idle 60 s) to reclaim work from dead consumers; poison messages moved to
   stream:{name}.dlq after 3 delivery attempts (tracked via the XPENDING delivery
   counter). Enable Redis AOF everysec in the Compose config.
3. Envelope + idempotency (TITAN-BCK-010): every message carries {event_id (uuid7),
   topic, occurred_at, actor, payload, trace_id}; consumers dedup on event_id via a
   seen:{consumer_group} set with 24 h TTL, so replaying a whole stream into a
   fresh group produces no duplicate side effects.
4. Transactional outbox (TITAN-BCK-005): a writer helper that appends the event row
   in the SAME DB transaction as the state change, and a background drainer that
   publishes unpublished rows (via idx_outbox_unpublished) to Redis Streams and
   stamps published_at, idempotent by event UUID. Add a minimal demo worker service
   to Compose that consumes stream:events.domain to prove the full loop.

No TODOs in backend code.

Definition of done: killing the process between DB commit and publish loses no
events — the re-drain test recovers them; duplicate delivery is a proven no-op; a
killed consumer's pending message is reclaimed by another consumer in under 60 s;
a poison message lands in the DLQ after exactly 3 attempts.
```

### Phase 11 — Domain modules I: projects, milestones, sprints, tasks

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 7, 9

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 7 (REST conventions framework: UUIDv7, cursor pagination, problem+json, ETag/If-Match, Idempotency-Key, ordered middleware); 9 (RBAC with the Requires() dependency, AST lint, ownership checks). Phase 6 delivered the schema including the task_dependencies recursive-CTE cycle check.

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md (TITAN-FND-001 — free-text brief creates a Project)
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §4.2–4.3 (endpoint map + example payloads) and §6 (schema fields these endpoints expose)

Build PHASE 11 — the project/task spine API:
1. projects module: POST /projects accepts the §4.3 payload ({name, display_name,
   brief (free text), mode, llm_profile}) and returns 201 with a UUIDv7 id, status
   "intake", created_by, version 1 (TITAN-FND-001); GET /projects (cursor
   pagination, ?status= filter, sort=-created_at); GET/PATCH /projects/{id} with
   ETag/If-Match concurrency; POST /projects/{id}:archive (AIP-136 :verb style);
   plus the aggregates GET /projects/{id}/board (kanban columns from task statuses)
   and GET /projects/{id}/graph (dependency graph nodes + edges).
2. milestones and sprints CRUD nested under their project, per the §6 shapes:
   milestone {title, due_at, status}, sprint {number UNIQUE per project, goal,
   starts_at, ends_at, status planned|active|closed}.
3. tasks module: POST /projects/{id}/tasks, GET /projects/{id}/tasks, GET
   /tasks/{id}, PATCH /tasks/{id} — the PATCH schema MUST NOT accept a status
   field: state moves only through the future :transition endpoint (§4.2), so
   reject status changes with a 422 problem. POST /tasks/{id}/dependencies runs
   the cycle check and returns 409 with problem type
   https://titan.dev/errors/invalid-dependency-cycle on a cycle.
4. Module discipline: each module exposes exactly one public interface
   (service.py with Pydantic v2 DTOs); routers and repositories stay private
   (TITAN-BCK-004).

Build every endpoint ON the Phase 7 conventions framework and protect every route
with the Phase 9 Requires() dependency — do not hand-roll per-module pagination,
error, or auth variants. Reuse the Phase 6 recursive-CTE cycle check — do not
implement a second cycle detector. No TODOs in backend code.

Definition of done: every endpoint has happy-path + auth-failure +
validation-failure integration tests; POST /projects returns 201 with an id and
the project appears in the /board aggregate; a stale ETag on PATCH returns 412;
inserting an A→B→C→A dependency chain through the API returns 409 end-to-end.
```

### Phase 12 — Domain modules II: agents, messages, decisions, runs, artifacts, notifications

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 11

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 11 (domain modules I — projects, milestones, sprints and
tasks CRUD, built on the REST conventions framework and the Phase-6 schema).

Read fully:
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §4.2–4.3
  (endpoint map + example payloads), §8.2 (run lifecycle), and TITAN-BCK-009
- Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — TITAN-FRT-038
  (notification persistence requirement)

Build PHASE 12 — the remaining domain modules:
1. Agents registry: GET /agents, POST /agents, GET /agents/{id}, PATCH /agents/{id},
   POST /agents/{id}:pause and :resume (all under /api/v1, per the §4.2 endpoint
   map). Fields per the Phase-6 agents table: role (the 14 roles: ceo|pm|
   project_manager|architect|backend|frontend|db|devops|qa|security|reviewer|docs|
   research|memory), model_binding jsonb, config jsonb, status idle|busy|paused|
   error, current_task_id. :pause/:resume flip status with proper 409s on invalid
   states (e.g. resuming a non-paused agent).
2. Messages: GET /projects/{id}/messages?thread_id=…&kind=… and
   POST /projects/{id}/messages (human → agent). thread_id is UUIDv7 grouping a
   conversation; kind is one of the 9 kinds (task_assignment, progress_report,
   review_request, architecture_discussion, bug_report, decision_log,
   meeting_summary, conflict_resolution, human); body is jsonb validated through a
   per-kind body-schema hook — register a permissive default per kind now so
   Volume 2's strict schemas can plug in later without touching the router. Enforce
   the sender check (agent XOR/OR user sender must be present).
3. Decisions: GET /projects/{id}/decisions and POST /projects/{id}/decisions with
   the full ADR field set from the schema — title, context, decision, alternatives,
   consequences, status (proposed|accepted|superseded), decided_by_agent_id, and
   supersedes_id; recording a superseding decision flips the old one to superseded.
4. Runs + artifacts: POST /tasks/{id}/runs, GET /runs/{id}, GET /runs?task_id=…,
   POST /runs/{id}:cancel, GET /runs/{id}/artifacts. Runs carry attempt, status
   (queued|running|succeeded|failed|cancelled|timed_out), input jsonb, provider/
   model, tokens_in/out, cost_usd. Artifact rows (kind: code_patch|file|test_report|
   doc|diagram|build_log, path, sha256, size_bytes) resolve their storage_ref
   through a Storage protocol (interface) with a filesystem implementation at MVP —
   never raw file paths in handlers, so object storage can swap in later.
5. Notifications: GET /notifications (cursor-paginated, unread filter),
   POST /notifications/{id}:mark-read, POST /notifications:mark-all-read — served
   from the notifications table created in the Phase-6 migrations. Per
   TITAN-FRT-038 these are persisted server-side so the bell badge and unread count
   survive reload and restart.

Extend the routers, cursor pagination, RFC 9457 problem responses, and :verb action
conventions already used by the Phase-11 modules — do not build a second framework.
No TODOs in backend code.

Definition of done: every endpoint above has integration tests for happy path, auth
failure, and validation failure; import-linter module-boundary contracts still pass;
a test marks a notification read, restarts the app against the same DB, and asserts
the read state persisted.
```

### Phase 13 — Task transitions, scheduler & race-safe assignment

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 10, 12

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 10 (events infra — QueueBus protocol over Redis Streams,
consumer groups, and the transactional outbox publisher), 12 (all domain modules:
agents, messages, decisions, runs, artifacts, notifications).

Read fully: Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md
§8.1–8.2 (task state machine + run lifecycle) and TITAN-BCK-012 through
TITAN-BCK-016.

Build PHASE 13 — the task orchestration engine:
1. POST /tasks/{id}:transition as the ONLY way task state moves (no free-form PATCH
   status). Server-side transition table over the 9 states (backlog, ready,
   assigned, in_progress, in_review, changes_requested, done, reopened, cancelled)
   keyed {(from_status, action) → to_status, required_permission,
   required_actor_role} per TITAN-BCK-012. Every legal transition writes a messages
   row (kind matching the action) and an outbox event task.{action}; illegal pairs
   return 409 with the invalid-transition RFC 9457 problem type from §4.3.
2. Scheduler per TITAN-BCK-013/014: the ready pool is ordered (priority ASC,
   blocked_dependents_count DESC, created_at ASC) — critical first, unblockers
   next, FIFO ties. A task enters ready only when all its depends_on tasks are
   done; completing a task re-evaluates its dependents INSIDE the same transaction
   and XADDs newly-ready tasks to stream:tasks.ready so a worker sees them within
   1 s locally.
3. Race-safe assignment per TITAN-BCK-015: match ready tasks to agents by role
   capability and load (max 1 concurrent run per agent at MVP), acquiring
   lock:task:{id} via SET NX PX 30000 with heartbeat renewal before the assigned
   transition.
4. Review & reopen per TITAN-BCK-016: every task's reviewer must be distinct from
   its assignee (Code Reviewer agent by default); approve → done; request_changes →
   changes_requested with a structured review message; reopen (from done) requires
   the task:reopen permission and creates a new run attempt with attempt = max+1.

Publish all events through the Phase-10 transactional outbox and QueueBus — never
XADD directly from domain code; that is the only queue mechanism in the codebase.

Definition of done: exhaustive unit tests cover every (state, action) pair with
illegal pairs returning 409; a 100-iteration race test with two concurrent claimers
of one task yields exactly one assigned winner; an integration test completes the
last blocker and asserts the dependent turned ready in the same transaction and was
enqueued on stream:tasks.ready; an end-to-end test proves a task where assignee ==
reviewer is rejected and cannot reach done.
```

### Phase 14 — Worker process & run lifecycle

**Milestone:** M2 — Data spine & walking skeleton · **Size:** S · **Depends on:** 13

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 13 (server-side transition table, ready-pool scheduler,
race-safe assignment over stream:tasks.ready via the QueueBus and outbox).

Read fully: Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md
§8.2 (run lifecycle) and TITAN-BCK-016/017.

Build PHASE 14 — the titan.worker process:
1. A runnable worker (python -m titan.worker, own Compose service) consuming
   stream:runs.execute through the QueueBus consumer group. Per TITAN-BCK-017 it
   executes runs concurrently in an asyncio task group bounded by
   WORKER_CONCURRENCY (default 4), across distinct agents within one worker and
   across worker replicas. The run executor is a pluggable protocol — ship a stub
   executor now (real LLM turns arrive with the agent runtime) that resolves the
   task spec, produces an artifact through the Phase-12 Storage protocol, and
   reports token/cost figures.
2. Full lifecycle per §8.2: pop → run status running (started_at set) → execute →
   write artifact rows + token/cost accounting on the run row → publish result to
   stream:runs.results. The orchestrator consumes runs.results and drives the
   Phase-13 state machine (success → submit_review).
3. Failure handling: soft timeout 15 min (configurable) → run status timed_out;
   retriable twice with exponential backoff, then the message moves to
   stream:runs.execute.dlq (delivery counter via XPENDING, per ADR-BCK-02). The
   XAUTOCLAIM reaper (min-idle 60 s) reclaims work from dead consumers.

Consume and ack exclusively through the Phase-10 QueueBus (XREADGROUP/XACK/
XAUTOCLAIM) and persist artifacts through the Phase-12 Storage protocol — do not
build second mechanisms for either. No TODOs in backend code.

Definition of done: fault-injection tests prove the timeout → retry ×2 → DLQ path
(run rows show timed_out, DLQ stream depth rises); killing a worker mid-run gets
the in-flight message reclaimed via XAUTOCLAIM and completed by a peer worker with
no duplicate side effects; a successful run consumed from runs.results yields
persisted run + artifact rows and transitions its task to in_review.
```

### Phase 15 — LLM Gateway core: Anthropic + Ollama + embeddings

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 1, 2

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 1 (monorepo scaffold and toolchain), 2 (Compose core
stack, FastAPI app factory, pydantic-settings config, health endpoint).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md —
  TITAN-FND-019/020 and ADR-007 (provider-agnostic gateway decision)
- Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md §8.1–8.2,
  TITAN-INT-002/060/061/062, and ADR-INT-03 (thin in-house gateway, not LiteLLM)

Build PHASE 15 — the LLM Gateway core in titan/gateway:
1. The canonical schema from §8.1, exactly: ChatRequest {model_alias, messages[],
   tools[]?, tool_choice?, temperature?, max_tokens, stream?, metadata{agent_id,
   task_id, project_id}} and ChatResponse {text?, tool_calls[]?, finish_reason,
   usage{input_tokens, output_tokens, cached_tokens}, provider, model_id, cost_usd,
   latency_ms}. Messages are provider-neutral (system/user/assistant/tool roles;
   content parts for text, image, tool_use, tool_result); tool definitions are
   canonical JSON Schema; streaming is normalized to ONE internal delta-event shape
   regardless of provider.
2. Embeddings: EmbeddingsRequest/Response models and the gateway /embeddings route,
   with an Ollama embedding adapter defaulting to nomic-embed-text-v1.5 (768-d,
   8192-token context) — the embedding provider/model swaps by configuration only,
   never code (this route later feeds the Volume-5 ingestion pipeline).
3. Two chat adapters with tool-call normalization per TITAN-INT-061, including
   parallel tool calls: Anthropic (Messages API) and Ollama (OpenAI-compatible
   endpoint). Adapters translate to/from the canonical format; quirks stay inside
   the adapter.
4. config/models.yaml per §8.2: providers block (secrets by ${ENV_VAR} reference),
   model_aliases, routing.default plus routing.by_agent (e.g. agent.ceo →
   reasoning-large), and fallback chains (e.g. reasoning-large: [coding-default,
   local-default]) — parsed and validated now, fallback execution lands in
   Phase 17. Hot-reloadable without restart. Ship a local-only profile
   (routing.default: local-default via Ollama) that needs zero paid API keys.
5. One internal async Python interface plus an internal HTTP endpoint. Enforce the
   TITAN-INT-002 chokepoint: no provider SDK import outside titan/gateway/** —
   add an import-linter contract that fails the build on violation.

Definition of done: a golden multi-tool, multi-turn transcript test passes against
live Ollama (skippable if Ollama is absent) and against Anthropic via a local
wire-fixture mock (recorded-cassette conformance comes next phase); an embeddings
round-trip on the local-only profile returns 768-d vectors with zero paid keys
configured; a test proves switching provider requires editing models.yaml only;
the import-linter chokepoint contract passes.
```

### Phase 16 — OpenAI + Google adapters + conformance matrix

**Milestone:** M2 — Data spine & walking skeleton · **Size:** S · **Depends on:** 15

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 15 (gateway core — canonical chat/tool/streaming/
embeddings schema, Anthropic + Ollama adapters, models.yaml routing, INT-002
chokepoint).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§8.2 and TITAN-INT-060 through TITAN-INT-062.

Build PHASE 16 — the remaining cloud adapters and the conformance harness:
1. OpenAI adapter (Chat Completions + Responses APIs) and Google adapter (Gemini
   API), translating to/from the Phase-15 canonical format with full tool-call
   normalization — including parallel tool calls and provider-specific quirks such
   as OpenAI strict-mode schemas (TITAN-INT-061). No canonical-schema changes to
   accommodate a provider; quirks live in the adapter.
2. A record/replay cassette harness: adapter wire traffic can be recorded once
   against a live provider (keys present) and replayed deterministically in CI
   (no keys, no network). Cassettes are committed fixtures; secrets are scrubbed
   at record time.
3. The four-backend conformance matrix per TITAN-INT-060: the Phase-15 golden
   multi-tool, multi-turn transcript (including streaming) runs against Anthropic,
   OpenAI, Google, and Ollama backends selected by swapping models.yaml alone,
   with golden-file assertions on the round-tripped canonical transcript per
   adapter.
4. A live-provider conformance run wired as a separate, non-blocking CI job
   (skipped when keys are absent) so real API divergence is caught without
   breaking the main pipeline.

Extend the Phase-15 adapter interface and golden-transcript fixtures — do not fork
a second transcript format or test harness.

Definition of done: the golden transcript passes on all 4 providers via cassettes
in a keyless environment; a test proves the backend under test is selected by
models.yaml config swap alone, with no code change between matrix legs.
```

### Phase 17 — Gateway resilience, cost accounting & budgets

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 16

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 16 (four provider adapters, record/replay cassette
harness, four-backend conformance matrix).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md —
  TITAN-FND-021 (per-call cost metering) and TITAN-FND-053 (graceful degradation)
- Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md §8.3–8.4,
  TITAN-INT-063/064
- Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §10 and
  TITAN-OPS-021 (budget hierarchy, 80%/100% behavior)

Build PHASE 17 — resilience, metering, and hard budget caps:
1. Resilience per §8.3: per-request retries with jittered backoff, then fallback
   along the models.yaml fallback chain on the exact trigger set — provider 5xx/
   timeout (after retries), 429 rate limit, context-window overflow (falls back
   ONLY to a larger-context alias), and budget-cap breach. Per-provider circuit
   breaker: open after 5 failures/30 s, half-open probes. Every fallback is
   recorded on ChatResponse metadata and the audit trail, and results served by a
   fallback model are tagged degraded_model=true (Volume 9 §10) so later review
   logic can tighten.
2. Accounting per §8.4: every ChatResponse.usage written to a new llm_usage table
   (timestamp, project, task, agent, provider, model, input/output/cached tokens,
   cost_usd) with cost computed from a versioned config/prices.yaml updatable
   without deploy — Ollama priced at 0. Export Prometheus metrics
   titan_llm_tokens_total, titan_llm_cost_usd_total, titan_llm_latency_seconds
   labeled by provider/model/agent.
3. Budgets per TITAN-INT-064 and TITAN-OPS-021: Redis rolling counters for
   per_project_usd and per_task_usd checked PRE-request (estimated prompt tokens ×
   price table) and settled post-call with actual usage; action_on_exceeded:
   fallback_then_pause — fall back to local, else set budget_hold, a persisted
   pause flag at gateway/project level that rejects new LLM calls at 100% and
   notifies at 80%. (Wiring budget_hold into the task state machine comes in a
   later phase — here it must persist and block gateway calls.)

Extend the Phase-16 cassette harness for the chaos and reconciliation tests, and
the Phase-15 models.yaml budgets block — do not invent a second config surface.

Definition of done: a chaos test blackholes the primary provider mid-conversation
(wire-fixture mock) and the conversation completes on the fallback chain with the
substitution visible in response metadata and audit records; a test drives a task
past per_task_usd and asserts budget_hold is set pre-request, with no provider call
exceeding the cap by more than one maximal request; metered llm_usage totals
reconcile against provider-reported usage embedded in recorded cassettes within 2%.
```

### Phase 18 — Tool Registry + Tool Bus pipeline

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 9, 15

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 9 (RBAC engine with roles, scopes, route protection and
ownership checks), 15 (LLM Gateway core with Anthropic + Ollama adapters, embeddings,
and the canonical JSON-Schema tool-definition format of Volume 7 §8.1).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§4.1–4.5 (Tool Registry, invocation flow, sandboxing, risk classes, audit — incl.
TITAN-INT-001/020/022/023) and Project-TITAN-Docs/08-Volume-8-Security.md §4.3–4.4
(agent tool allowlists, TITAN-SEC-031).

Build PHASE 18 — the Tool Registry and Tool Bus, the single chokepoint every agent
side-effect must pass through:
1. Tool Registry: an in-memory index rebuilt on boot from PostgreSQL + provider
   modules, mapping namespaced tool names (fs.read, git.commit, mcp.<server>.<tool>)
   to: JSON Schema draft 2020-12 input schema AND declared output schema, provider
   (builtin module / plugin id / MCP server id), risk class (read/write/admin),
   required RBAC scopes, timeout_s, and rate limits — matching the git.commit
   registry-entry example in §4.1. Per turn, an agent's tool list is (agent role's
   allowed tools) ∩ (enabled providers) ∩ (project-level policy), emitted in the
   Phase-15 gateway's canonical tool-definition format.
2. Tool Bus pipeline, exactly the 7 steps of §4.2: (1) parse & schema-validate args,
   (2) authz — agent scopes ∩ tool scopes, project policy, risk gate, plus the
   TITAN-SEC-031 parameter constraints (allowlist AND task project scope AND
   per-parameter rules such as git writes only to titan/task-* branches),
   (3) write tool_audit row status=STARTED, (4) dispatch to the executor,
   (5) enforce timeout and capture stdout/stderr/artifacts, (6) schema-validate the
   result and run a pluggable secret-redaction hook, (7) finalize the audit row,
   emit an OpenTelemetry span, return the tool_result. Persist and transmit the
   exact call/result envelopes from §4.2 (tool_call_id, trace_id, project_id,
   task_id, agent_id, tool, arguments, requested_at; status, duration_ms, result,
   stderr_tail, artifacts).
3. Error taxonomy: failures return status "error" with machine-readable code —
   SCHEMA_INVALID, PERMISSION_DENIED, TIMEOUT, SANDBOX_VIOLATION, UPSTREAM_ERROR,
   TOOL_UNAVAILABLE — plus an LLM-readable message (schema-pointer for validation
   errors) fed back to the calling agent for self-correction.
4. The tool_audit table per TITAN-INT-023: tool_call_id, trace_id, project, task,
   agent, tool, argument hash + redacted args JSONB, status, duration, result hash,
   initiator chain; INSERT-only (immutable), with prev_hash chaining per project
   (TITAN-INT-024). Plus the TITAN-INT-001 CI lint rule rejecting subprocess,
   open() in write mode, and raw HTTP clients anywhere under titan/agents/**.

Enforce authorization with the Phase-9 RBAC engine and scopes — do not build a
second permission system. Emit tool definitions through the Phase-15 gateway format
— do not invent a parallel schema dialect. No TODOs in backend code.

Definition of done: malformed arguments are rejected pre-execution with
schema-pointer error messages (unit tests per error code); every executed call has
a tool_audit row and an OTel span, proven by integration test; the CI lint fails on
a planted subprocess call in titan/agents/**.
```

### Phase 19 — Risk classes, approval gate & tool fuzz campaign

**Milestone:** M2 — Data spine & walking skeleton · **Size:** S · **Depends on:** 18

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 18 (Tool Registry + Tool Bus pipeline with schema
validation, RBAC authz, error taxonomy, and the immutable tool_audit table).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§4.3–4.5 (risk classes and approval gates, TITAN-INT-022/023) and
Project-TITAN-Docs/08-Volume-8-Security.md §8 (TITAN-SEC-073 tool-argument
constraint checking).

Build PHASE 19 — risk gating and the adversarial fuzz campaign:
1. Wire the read/write/admin risk classes into the Tool Bus authz step with the
   default gating table from Volume 7 §4.4: read auto-approved; write auto-approved
   within workspace/project scope and rate-limited; admin blocks on an approval
   record (human or policy-delegated) BEFORE dispatch per TITAN-INT-022. An
   unapproved admin call returns PERMISSION_DENIED and creates a pending-approval
   record with a human resolution path (approve/deny endpoints that release or
   reject the call), queryable by the dashboard.
2. A fuzz suite that fires malformed arguments at every tool in the registry
   (missing required fields, wrong types, extra properties, oversized strings)
   plus the TITAN-SEC-073 adversarial corpus — ../../etc/passwd path traversal,
   "; rm -rf /" shell metacharacters, internal/metadata URLs — asserting per
   TITAN-INT-020 that 100% are rejected pre-execution with schema-pointer messages
   and that every rejection is audited.

Extend the Phase-18 Tool Bus authz step and tool_audit table — do not add a second
gating mechanism or audit path. No TODOs in backend code.

Definition of done: an admin-risk call without approval yields PERMISSION_DENIED
plus a pending-approval record, and succeeds after approval (integration test);
the fuzz suite reports 100% pre-execution rejection across every registered tool.
```

### Phase 20 — Workspace sandbox + fs tools

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 19

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 19 (risk classes, admin approval gate, and the fuzz
campaign on top of the Phase-18 Tool Bus).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§4.3 and §5 (sandbox policy table, core tool set, TITAN-INT-021/031) and
Project-TITAN-Docs/08-Volume-8-Security.md §5 (agent execution sandboxing,
TITAN-SEC-040/041/042/043/044, ADR-SEC-02).

Build PHASE 20 — the hardened per-project workspace sandbox and the fs.* tools:
1. Tool Host per-project workspace container matching the Vol 8 §5 diagram and the
   Vol 7 §4.3 policy table: workspace mounted rw at /workspace, everything else
   read-only or absent; network deny-all egress; non-root uid 10001; ALL
   capabilities dropped; no-new-privileges; read-only rootfs (writable /workspace
   and /tmp only); seccomp at least runtime-default; cgroup limits CPU ≤ 2,
   memory ≤ 2 GiB, PIDs ≤ 256, disk ≤ 5 GiB, wall-clock ≤ task timeout (default
   30 min), with the §4.3 per-call default of 1 vCPU / 1 GiB / 60 s and per-tool
   registry overrides; never mounts the Docker socket or any platform credentials
   (SEC-044). Hardened runc locally per ADR-SEC-02; per-task create/destroy so
   sequential tasks get distinct containers and clean workspaces (SEC-040).
2. fs.read / fs.write / fs.list / fs.glob / fs.grep / fs.patch registered on the
   Tool Bus per the Vol 7 §5 table: all paths canonicalized and confined to
   /workspace (TITAN-SEC-073); fs.patch applies unified diffs atomically —
   all-or-nothing, rejecting malformed or non-applying diffs.
3. An adversarial test suite for jail, escape, and resource limits: path escapes
   (../../etc/passwd, absolute paths, symlink tricks), hostile filenames, the
   TITAN-INT-021 red-team probes (cat /etc/shadow, socket probing, docker.sock
   mount attempt, env-var secret scraping), fork-bomb and memory-hog kills.

Register every tool through the Phase-18 Tool Registry and route every call through
the Tool Bus — no direct filesystem access path may bypass it. No TODOs in backend
code.

Definition of done: path-escape and hostile-filename attempts stay jailed or are
treated as literal names; fork-bomb and memory-hog are killed by cgroup limits with
no host impact; a host-filesystem read attempt is blocked and produces a
SANDBOX_VIOLATION audit event; fs.patch is proven atomic and rejects malformed
diffs by test.
```

### Phase 21 — git, terminal & code tools

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 20

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 20 (per-project workspace sandbox with jailed fs.* tools
on the Tool Bus).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§4.1 (git.commit registry-entry example) and §5 (core tool set table,
TITAN-INT-030 partial, TITAN-INT-031).

Build PHASE 21 — the git, terminal, and code tool namespaces inside the sandbox:
1. git.status / git.diff / git.log / git.branch / git.commit / git.merge /
   git.rebase / git.push / git.clone operating on the local repo in /workspace
   (remotes only via connector credentials, so push/clone against local fixtures
   for now). git.commit must match the §4.1 registry entry exactly (message
   minLength 10, paths array, author_agent; output commit_sha + files_changed);
   enforce the TITAN-SEC-031 parameter constraint that git writes only touch
   titan/task-* branches.
2. terminal.run / terminal.run_background / terminal.kill per TITAN-INT-031:
   argv arrays only — no shell string interpolation — with env, cwd, and timeout;
   output capped at 1 MiB per stream with an explicit truncation marker;
   incremental output streamed as tool.output events on Redis pub/sub for the
   dashboard Logs view; run_background returns a handle that kill terminates.
3. code.run_python / code.run_node / code.lint / code.test as convenience wrappers
   over terminal.run with structured result parsing: code.test parses JUnit XML
   and coverage output into a typed result object.

Register all tools in the Phase-18 Tool Registry with full input/output JSON
Schemas and run them inside the Phase-20 sandbox — do not spawn processes outside
the Tool Host. No TODOs in backend code.

Definition of done: hostile-input tests pass (hostile filenames execute literally
as argv; a `yes`-spam command is truncated at 1 MiB and killed at timeout);
terminal output streaming over Redis pub/sub is proven by an integration test that
subscribes and sees incremental tool.output events; the git flow branch → commit →
merge completes inside the jail; code.test parses JUnit + coverage from a fixture
repo.
```

### Phase 22 — docker & browser tools

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 20

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 20 (per-project workspace sandbox with jailed fs.* tools;
Tool Bus + registry from Phase 18 with risk gating from Phase 19).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§4.3 (Docker access policy) and §5 (core tool set table, TITAN-INT-030/032) and
Project-TITAN-Docs/08-Volume-8-Security.md §5 (TITAN-SEC-042 resource limits,
SEC-044 no host socket).

Build PHASE 22 — the docker and browser tool namespaces:
1. docker.build / docker.run / docker.compose_up / docker.compose_down /
   docker.logs / docker.ps talking exclusively to a rootless DinD (or sysbox)
   sidecar daemon per workspace — per Vol 7 §4.3 the host Docker daemon socket is
   never mounted or reachable (TITAN-SEC-044). Containers started by these tools
   inherit the workspace resource ceilings (SEC-042) and the deny-all egress
   posture.
2. browser.goto / browser.snapshot / browser.screenshot / browser.click /
   browser.type / browser.eval_js via Playwright driving headless Chromium in its
   own container (not the Tool Host). browser.snapshot returns the token-efficient
   accessibility-tree representation (role/name/ref) per TITAN-INT-032 — under 10%
   of the raw HTML token count — with browser.screenshot available for
   vision-capable models; click/type target elements by snapshot ref.

Register both namespaces in the Phase-18 Tool Registry with full input/output JSON
Schemas and risk class write, dispatched through the Tool Bus — do not create a
second execution path. No TODOs in backend code.

Definition of done: docker.compose_up of a fixture app makes it reachable from the
test while the host Docker socket is untouchable from inside the sandbox (mount
probe blocked and audited); a browser snapshot → click → snapshot round-trip on a
fixture page shows the post-click state change.
```

### Phase 23 — web tools + registry mirror + egress proxy

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 20

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 20 (per-project workspace sandbox with deny-all egress
and jailed fs.* tools; Tool Bus + registry + risk gating from Phases 18–19).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§5 (core tool set table, TITAN-INT-030) and
Project-TITAN-Docs/08-Volume-8-Security.md §5 (TITAN-SEC-042/043 — sandbox limits
and deny-all egress via authenticating, logging forward proxy).

Build PHASE 23 — research web tools plus the sole audited egress/install path:
1. web.search and web.fetch (risk read) per the Vol 7 §5 table: search through a
   pluggable backend with self-hosted SearXNG as the free default (provider APIs
   config-swappable); web.fetch returns readability-extracted markdown, not raw
   HTML. All traffic egresses via a caching research proxy added to Docker
   Compose — the tools never open direct connections from the sandbox.
2. A caching pypi/npm registry mirror plus an authenticating, logging forward
   egress proxy as the ONLY paths through the sandbox's deny-all network, per
   TITAN-SEC-043: from inside the sandbox only the registry mirror and the task's
   git remote are reachable; every proxied request is logged with project/task
   attribution; direct egress remains blocked and requires Admin opt-in per
   project (SEC-033 approval flow from Phase 19) to relax.

Register web.* in the Phase-18 Tool Registry with full input/output JSON Schemas
and dispatch through the Tool Bus; blocked-egress attempts must surface as
SANDBOX_VIOLATION audit events — reuse the Phase-18 taxonomy and tool_audit table.
No TODOs in backend code.

Definition of done: pip install and npm install inside the sandbox succeed only via
the mirror, with proxy log entries, while a direct in-sandbox
`curl https://example.com` fails and is audited; a web.search + web.fetch
round-trip works through the research proxy and a repeated fetch is served as a
cache hit (proven by test).
```

### Phase 24 — WebSocket realtime backend

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 9, 10, 12

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 9 (RBAC engine with route protection and project-scoped
permission checks), 10 (events infra: QueueBus protocol, Redis Streams consumers,
transactional outbox drained by a background publisher), 12 (domain modules for
agents, messages, decisions, runs, artifacts, notifications — all emitting outbox
events).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md —
  TITAN-FND-023 and TITAN-FND-050 (§4.7/§5) and ADR-009 (§11.2)
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md — §5
  "WebSocket Event Protocol" (TITAN-BCK-007) and the WS row of the §12 rate-limit
  table
- Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §5.2 "Core topics and
  events" (the topic names the frontend will subscribe to)

Build PHASE 24 — the JWT-authenticated multiplexed WebSocket backend:
1. FastAPI endpoint `/ws?token=<jwt>` (ADR-009): validate the RS256 access token at
   handshake (same JWT machinery as REST — check the Redis jti denylist), enforce
   the connect-time cap of 5 concurrent connections per user from the Volume 3 §12
   table. Client→server frames: `subscribe {project_id, topics[], last_seq?}`,
   `unsubscribe`, `ping`. Subscribing to a project topic requires the caller to
   pass the Phase-9 RBAC check for that project — reject with a typed error frame
   otherwise. Every server frame uses the fixed envelope
   {v, type, seq, ts, project_id, payload} and implements all 8 server event types
   from the Volume 3 §5 table: task.created/task.updated, run.started/run.finished,
   run.output.delta, message.created, decision.recorded, agent.status,
   sprint.updated, sys.notice.
2. Fan-out via Redis pub/sub so it works across multiple api replicas: a consumer of
   the Phase-10 outbox/event stream publishes domain events to
   `ws.project.{project_id}` (TITAN-BCK-007), plus the Volume 4 §5.2 scoped
   channels: `notifications:{userId}` (user-scoped — delivered ONLY to that user's
   connections), `org:{oid}:activity`, and `agents:status`. Each api replica
   subscribes to Redis pub/sub and forwards to its local sockets.
3. Reconnect and liveness: `seq` is monotonically increasing per connection; buffer
   the last ≤1,000 events per project in a Redis list `ws.buffer.{project_id}`
   (capped, TTL 15 min). On `subscribe` with `last_seq` inside the buffer, replay
   the gap in order before live events; beyond the buffer, send
   {"type":"sys.resync"} so the client refetches via REST. Server pings every 25 s
   and closes after 2 missed pongs. Token expiry or revocation closes the
   connection ≤60 s (TITAN-SEC-016) unless re-authenticated in-band.
4. A minimal test client (script/pytest fixture, NOT in apps/frontend) that
   connects, subscribes, and asserts the definition-of-done scenarios — the
   production frontend client lands later with the dashboard data layer.

Extend the Phase-10 outbox/QueueBus consumers to publish WS events — do not build a
second event path from domain code to sockets. Reuse the existing JWT
validation/denylist from the identity layer — no parallel auth for WS.

No TODOs in backend code; integration tests for every frame type and every failure
mode (bad token, over-cap connect, unauthorized subscribe).

Definition of done: two connected clients subscribed to the same project both
receive a task.updated frame within 250 ms of the transition commit, proven with
fan-out across 2 api replicas in Compose; a client reconnecting with last_seq inside
the buffer window replays the gap in order, and one reconnecting beyond it receives
sys.resync; revoking a token closes the live connection in under 60 s; a
notification published to notifications:{userId} reaches only that user's
connection, verified with two users connected simultaneously.
```

### Phase 25 — Walking skeleton: CEO brief → stories, live in UI

**Milestone:** M2 — Data spine & walking skeleton · **Size:** M · **Depends on:** 4, 5, 11, 15, 17, 24

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 4 (observability foundation in Compose: OTel collector,
Tempo, Prometheus, Loki, Grafana), 5 (dashboard shell with 13-surface navigation and
design system), 11 (domain modules for projects, milestones, sprints, tasks —
POST /api/v1/projects exists), 15 (LLM Gateway core with Anthropic and Ollama
adapters and normalized streaming), 17 (gateway cost accounting: every ChatResponse
written to llm_usage with tokens and cost_usd), 24 (JWT-authenticated WebSocket with
Redis fan-out and ws.project.{id} topics).

Read fully:
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md — §9.1 MVP
  exit criteria ("The Loop Works")
- Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md — §8.1 unified
  gateway interface (ChatRequest/ChatResponse, streaming normalized to one internal
  delta-event shape re-broadcast over WebSockets)
- Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.2 Projects intake
  (TITAN-FRT-023: conversational intake yields a visible plan without a page reload;
  the wizard streams tokens)

Build PHASE 25 — one vertical slice proving the whole spine, end to end:
1. Minimal CEO agent handler: when POST /api/v1/projects receives a brief (the
   Volume 3 §4.3 payload: name, display_name, brief, mode, llm_profile), trigger a
   handler that sends the brief through the Phase-15 gateway as a ChatRequest
   (model_alias routed to a local Ollama model, stream=true, metadata carrying
   agent_id/project_id) and parses the response into ProductBacklog-style user
   stories ("As a…, I want…, so that…" with acceptance criteria, per Volume 6 H3),
   persisted via the Phase-11 domain modules. This is a deliberately thin slice of
   the real CEO agent (the full roster comes later) — keep it small and isolated so
   it can be replaced, but it still gets tests and has no TODOs.
2. Stream it live: as the model streams, publish run.output.delta frames on
   ws.project.{id} through the Phase-24 WebSocket, then the persisted-story events
   when rows commit. Add a minimal Projects/Overview view inside the Phase-5
   dashboard shell with a throwaway WS hook (clearly marked as such — the production
   data layer comes with the dashboard build-out) that shows tokens streaming and
   stories appearing with no reload.
3. Observability proof: the flow renders as ONE connected trace in Tempo —
   API request → gateway llm.call span (provider, model, tokens_in, tokens_out,
   cost_usd attributes) → DB writes — via the propagated trace_id (TITAN-BCK-025);
   the run's usage lands in llm_usage via the Phase-17 accounting (Ollama priced
   at 0, tokens still recorded).

Use the existing gateway, WS layer, and outbox event path from Phases 15, 24, and
10 — do not hand-roll a direct Ollama call, a second socket, or an ad-hoc event
emit for this slice.

Definition of done: submitting a brief in the browser produces user stories visible
on screen within seconds, streamed token-by-token, with no page reload; Tempo shows
the whole flow as one connected trace from API through gateway to DB; the run
appears in llm_usage with input/output token counts and computed cost.
```

---

## M3 — Agents talk

> **Milestone exit state:** Multi-agent exchange with failure ladder, conflict resolution, veto; crash-safe replay

### Phase 26 — AgentDefinition schema, 16-agent roster & registry

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 6

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 6 (full PostgreSQL schema + Alembic migrations, including
the agents table).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §2.1 (agent
anatomy), §2.3 (TITAN-AGT-001/006), §3.1 (roster + TITAN-AGT-008), and every
per-agent spec in §4.1–4.16.

Build PHASE 26 — AgentDefinition schema, 16-agent roster & registry:
1. Pydantic v2 `AgentDefinition` model with exactly the eight fields of §2.1:
   (a) agent_id / display_name / tier (0–3 or "cross") / supervisor; (b)
   system_prompt_template — a Jinja2 template reference, composed later in Phase 28;
   (c) authority — decides_alone and must_escalate decision-class lists; (d)
   memory_scopes — read/write grants over the Volume 5 scopes (short_term, project,
   organization, code_patterns, architecture, lessons_learned); (e) tool_grants —
   allow-list of tool-registry IDs (ungranted tools are invisible to the agent); (f)
   model_policy — tier (reasoning|standard|fast), temperature, max output tokens,
   structured-output schema; (g) kpis — Prometheus metric names; (h) failure_policy —
   overrides to the §2.2 recovery ladder. Strict validation: a missing field fails.
2. Sixteen agents/*.yaml files, one per §3.1 roster row with the exact agent_ids,
   tiers, and supervisors: ceo (tier 0, human client), orchestrator / pm_product /
   architect (tier 1, ceo), pm_project (tier 2, orchestrator), eng_backend /
   eng_frontend / eng_database / eng_devops / qa / security / docs (tier 3,
   pm_project), reviewer + critic (cross, architect), research + memory (cross,
   orchestrator). Transcribe each agent's decides_alone / must_escalate lists,
   memory-scope deltas, tool grants, KPIs, and failure-policy deltas verbatim from
   its §4.x spec — do not invent or omit entries.
3. YAML loader with startup validation (bad roster = refuse to boot) and in-process
   caching, plus a registry service that exposes the loaded roster to the runtime.
   Expose it over HTTP through the agents endpoints that already exist from Phase 12
   — extend those routes, do not add a parallel agents API.
4. Prove TITAN-AGT-006 extensibility: add a sample Localization Agent as a new YAML
   file (plus prompt-template stub) only — zero changes to core/orchestrator code.

Definition of done: all 16 YAMLs validate at startup; a schema test that deletes a
required field from any YAML fails validation; a snapshot test asserts each agent's
decides_alone/must_escalate matches §4.1–4.16; the Localization Agent loads and
appears in the registry with zero core-code diffs. No TODOs in backend code.
```

### Phase 27 — Message schemas, persistence, DecisionLog & delivery

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 6, 10

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 6 (PostgreSQL schema + migrations, including messages,
decisions, and outbox tables); 10 (events infrastructure: QueueBus over Redis
Streams with the transactional-outbox publisher).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §6.1
(envelope), §6.2 (message types + JSON examples, TITAN-AGT-015), §6.3 (persistence
and delivery, TITAN-AGT-003/016), §7 (TITAN-AGT-019), and ADR-AGT-04 in §9.

Build PHASE 27 — message schemas, persistence, DecisionLog & delivery:
1. The `AgentMessage` envelope exactly per §6.1: uuid-v7 message_id, schema_version
   ("1.0"), type, project_id, task_id|null, thread_id|null, correlation_id
   (request/response pairing), from_agent, to_agent, priority
   (critical|high|normal|low), requires_ack, created_at, and a payload discriminated
   by type. Strict Pydantic: unknown type values rejected, unknown payload fields
   rejected — both logged.
2. The eight normative payloads with the fields shown in §6.2's JSON examples:
   TaskAssignment (title, description, acceptance_criteria, story_id, depends_on,
   estimate_points, deadline), ProgressReport (status in_progress|blocked|completed|
   failed, percent_complete, summary, blockers, artifacts), ReviewRequest (artifact
   type/ref, checklist, plus the gatekeeper verdict reply — decision approve|
   request_changes|reject|veto with line-anchored comments — reusing correlation_id),
   ArchitectureDiscussion (question, position, arguments, responding_to,
   decision_deadline), BugReport (severity, security flag, reproduction_steps,
   expected/actual, suspected_component, evidence), DecisionLog (decision, context,
   alternatives, trade_offs, authority, reversible), MeetingSummary (ceremony,
   participants, decision refs, action_items, summary), ConflictResolution
   (conflict_id, parties, subject, positions, round, arbiter, resolution). Posting
   a ninth type returns a validation error and is logged (TITAN-AGT-015).
3. Persistence and delivery per §6.3: every message is written to agent_messages
   (envelope columns + payload JSONB) BEFORE enqueue via the outbox; delivery to
   per-agent inbox streams titan:{project_id}:inbox:{agent_id} with one consumer
   group per agent runtime; at-least-once semantics with idempotent consumers keyed
   on message_id; requires_ack messages unacknowledged past their type-specific
   timeout re-enter the pending set and fire a watchdog hook (full watchdog lands in
   Phase 32 — expose the hook now).
4. DecisionLog immutability (TITAN-AGT-019): a database policy makes decision rows
   append-only — UPDATE/DELETE rejected at the DB layer, not just in app code. Every
   message emits an OpenTelemetry span linked by correlation_id.

Extend the Phase-10 QueueBus and transactional outbox to carry agent inboxes — do
not build a second delivery mechanism.

Definition of done: round-trip serialization tests for all 8 types pass against the
§6.2 example JSON; an unknown type or extra payload field raises a logged validation
error; a chaos test kills Redis mid-conversation and replay from PostgreSQL shows
zero missing messages (TITAN-AGT-016); SQL UPDATE/DELETE on a decision row is
rejected; an unacknowledged requires_ack message re-enters the pending set after its
timeout. No TODOs in backend code.
```

### Phase 28 — Prompt composition engine + 16 templates + model policy

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 26

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 26 (AgentDefinition schema, the 16-agent YAML roster, and
the registry service).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §8 (prompt
composition order, model-policy YAML, TITAN-AGT-021/022/026), the system-prompt
outlines in every §4.1–4.16 spec, and §10 risk R5 (authority drift).

Build PHASE 28 — prompt composition engine, 16 templates, model policy:
1. A Jinja2 composition engine producing the system prompt in the normative 7-part
   order of §8: (1) agent identity and mission; (2) canonical-stack and platform
   constraints; (3) authority section; (4) role heuristics and rubrics; (5) output
   JSON schema; (6) retrieved memory, provenance-tagged (empty placeholder until the
   memory phases); (7) current task/thread context. Templates are versioned in-repo
   so prompt changes go through normal PR review.
2. Sixteen system-prompt templates transcribing each agent's §4.x outline: e.g. CEO
   — prime directive "client value over technical elegance", clarification-question
   budget, explicit human-escalation triggers; Orchestrator — planning heuristics
   (small tasks, ≤1 owner each, explicit dependencies, mandatory acceptance
   criteria) and watchdog rules; QA — adversarial "your job is to break it" with
   criteria-cited verdicts; Security — OWASP checklist, severity rubric, "no silent
   risk acceptance"; Reviewer — severity-ordered rubric, "must cite the standard",
   anti-rubber-stamp; Critic — steelman-then-attack, "every weakness needs a
   concrete fix suggestion". Match every outline, not just these examples.
3. Authority drift guard (risk R5): the authority section is RENDERED FROM the
   agent's YAML decides_alone/must_escalate lists — single source of truth. No
   template may hard-code authority text.
4. config/model-policy.yaml exactly per §8: tiers reasoning/standard/fast; the
   local-free Ollama profile (qwen2.5-coder:32b / qwen2.5-coder:14b / llama3.2:3b);
   the per-agent map (ceo, architect, critic, security → reasoning; eng_*, reviewer,
   qa, pm_product, orchestrator → standard; pm_project, docs, research, memory →
   fast). Add a resolver that maps agent → tier → provider/model for the Phase-15
   gateway; switching provider/model must be config-only (TITAN-AGT-021), and no
   agent code imports provider SDKs directly (TITAN-AGT-022 — add the CI check if
   not already present).
5. Optional Critic second-provider pin (TITAN-AGT-026): a config flag that routes
   critic turns to a different provider than the artifact author, off by default.

Definition of done: a snapshot test shows a composed prompt with the 7 sections in
normative order; editing an agent YAML's authority list changes the rendered prompt
with no template edit; changing an agent's tier in model-policy.yaml resolves a new
provider/model with zero code changes. Tests for the resolver and every template
render. No TODOs in backend code.
```

### Phase 29 — Agent runtime turn loop + tool-grant enforcement

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 18, 27, 28

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 18 (Tool Registry + Tool Bus with schema-validated,
audited dispatch); 27 (AgentMessage envelope, 8 payloads, outbox-first persistence,
per-agent inbox streams); 28 (7-part prompt composition + model-policy resolver).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §8 (the
turn diagram), §2.2–2.3 (shared defaults, TITAN-AGT-002/004/024); and
Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md — §13 (memory flow
around one agent invocation — implement only the packet hook, stubbed).

Build PHASE 29 — the agent runtime turn loop with tool-grant enforcement:
1. The 7-step stateless turn from §8, executed by a pool of async workers:
   (1) XREADGROUP from titan:{project_id}:inbox:{agent_id}; (2) load the cached
   AgentDefinition from the Phase-26 registry; (3) hydrate context — composed system
   prompt (Phase 28), task + thread messages from PostgreSQL, memory retrieval
   behind a memory.search interface stubbed per Volume 5 §13 (the Memory Packet hook
   exists; real retrieval lands in the memory phases), and tool schemas for GRANTED
   tools only; (4) call the LLM through the gateway routed by model_policy — never a
   provider SDK directly; (5) validate structured output with Pydantic; (6) execute
   tool calls through the Tool Bus, then emit resulting messages via the outbox;
   (7) XACK + OTel span + KPI metric emission. Agents exist only during a turn
   (TITAN-AGT-002): kill/restart of the runtime must lose nothing accepted, because
   all durable state lives in PostgreSQL/Redis.
2. Structured-output failure triggers the self-critique retry hook — re-invoke with
   the validation failure appended, max 2 attempts — and is NEVER silently accepted
   (the full recovery ladder lands in Phase 30; wire the hook now).
3. Runtime tool-grant enforcement per TITAN-AGT-004: the allow-list is enforced at
   the runtime layer, not only in the prompt. Ungranted tool schemas are never sent
   to the model; a model-requested ungranted tool call is rejected and logged as a
   security event. Every executed tool call writes an audit row (agent, tool, args,
   result hash) per TITAN-AGT-024.

Consume messages via the Phase-27 inbox streams and idempotent-by-message_id
consumers — do not build a second consumption path.

Definition of done: an integration test drives a scripted two-agent exchange
(TaskAssignment → ProgressReport → ReviewRequest → DecisionLog) end-to-end through
Redis Streams using a scripted/mock provider, with every message persisted and
replayable; killing and restarting the runtime mid-exchange loses no accepted work
and produces no duplicate side effects; a prompt-injected request for an ungranted
tool is rejected and appears in the security-event log. No TODOs in backend code.
```

### Phase 30 — Failure-recovery ladder + KPI emission

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 4, 29

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 4 (observability foundation — OTel tracing, Prometheus,
Grafana, Loki in Compose); 29 (agent runtime turn loop with self-critique hook and
tool-grant enforcement).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §2.2 (the
default failure-recovery ladder, TITAN-AGT-005), the "Failure recovery" line of
every §4.1–4.16 spec (TITAN-AGT-010), §8 (TITAN-AGT-023), and §10 risk R4 (small
local models failing structured-output schemas).

Build PHASE 30 — the failure-recovery ladder and KPI emission:
1. The shared 4-rung ladder from §2.2, implemented in the runtime and configurable
   per agent: (1) automatic retry with same context on transient LLM/tool error, max
   2, exponential backoff; (2) self-critique retry — output failed schema validation
   or acceptance checks, re-invoke with the failure appended, max 2 (upgrade the
   Phase-29 hook into this rung); (3) escalate to supervisor via
   ProgressReport(status="blocked"); (4) supervisor reassigns, decomposes, or
   escalates to human. Every rung is logged and traced.
2. Per-agent failure_policy overrides driven by the YAML definitions, per §4.x: CEO
   has no supervisor agent — rung 3 goes directly to the human client with a
   structured options memo (§4.1); Database Engineer destructive operations skip
   retries and escalate immediately (§4.8); Security is fail-closed — merges
   requiring its sign-off queue rather than proceed (§4.11, TITAN-AGT-010), and
   Reviewer is fail-closed the same way (§4.12); Docs and Critic are fail-open —
   outages never block the pipeline, but are flagged/logged (§4.13, §4.16).
3. A fuzz suite of malformed LLM outputs (truncated JSON, wrong types, extra fields,
   schema-shaped prose — risk R4's local-model failure modes) proving TITAN-AGT-023:
   invalid output always follows retry-then-escalate, never silent acceptance.
4. KPI emission to Prometheus via OpenTelemetry: the universal KPIs of §2.2 (task
   success rate, first-pass acceptance rate, mean tokens per completed task, mean
   wall-clock latency per task, escalation rate) plus each agent's own kpis list
   from its YAML, labelled by agent_id.

Extend the Phase-29 runtime and the Phase-4 observability stack — do not add a
parallel retry wrapper or a second metrics pipeline.

Definition of done: fault-injection tests demonstrate all four rungs with one OTel
span per rung; a fuzzed malformed output retries then escalates and is never
silently accepted; KPI metrics are visible in Prometheus during a scripted exchange
run. No TODOs in backend code.
```

### Phase 31 — Conflict-resolution state machine + security veto

**Milestone:** M3 — Agents talk · **Size:** M · **Depends on:** 29, 30

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 29 (agent runtime turn loop over per-agent inbox streams);
30 (failure-recovery ladder with per-agent overrides and KPI emission).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md — §7
(conflict resolution and escalation, TITAN-AGT-017/018/019/020), §5 (collaboration
patterns, TITAN-AGT-012), §4.2 (Orchestrator, TITAN-AGT-009), §4.11 (Security
veto), and TITAN-AGT-011 in §4.

Build PHASE 31 — the conflict-resolution state machine and security veto:
1. A persisted state machine enforcing §7 rules 1–3 in the runtime — never left to
   prompt compliance (TITAN-AGT-017): (a) mandatory direct round — parties exchange
   one ConflictResolution round each with positions and evidence; (b) arbitration —
   no consensus after round 1 means the Orchestrator appoints the arbiter (Architect
   for technical disputes, PM Product for scope, PM Project for priority/
   assignment), and the arbiter's verdict is a binding DecisionLog; (c) executive
   escalation — disputed arbitration or cross-domain disputes go to the CEO, whose
   decision is final inside the platform. Every transition is persisted.
2. Security veto per §7.4 (TITAN-AGT-018): a Security `veto` verdict blocks
   merge/release at the pipeline level regardless of any other approval, including
   the CEO's. Only the human client (via the CEO relaying a structured risk memo)
   can override, and the override is recorded as a permanent DecisionLog with
   authority: "human_override" (append-only per TITAN-AGT-019).
3. Deadlock rule per §7.5 (TITAN-AGT-020): any conflict reaching round 3 without
   resolution, or any task reopened 3 times, auto-escalates one level and is flagged
   for the dashboard. Thresholds live in config and take effect without redeploy
   (the full Orchestrator watchdog lands in Phase 32 — expose the detection hook and
   config now).
4. Per-agent authority contract tests (TITAN-AGT-011) covering all 16 agents: the
   mechanically checkable rules — Security veto; co-signs (destructive migrations
   need PM + DevOps co-sign per §4.8, secret-handling design needs Security co-sign
   per §4.9, security-relevant design co-signed by Security per §4.4); and
   fail-open/fail-closed behavior per agent (Security/Reviewer closed, Docs/Critic
   open).

Drive all conflict traffic through the Phase-27 ConflictResolution and DecisionLog
message types and the Phase-29 runtime — do not invent a separate dispute channel.

Definition of done: a simulated dispute test traverses direct → arbitration →
executive with persisted state transitions; a vetoed artifact cannot be merged by
any path, including a CEO-approved release job; authority contract tests pass for
all 16 agents. No TODOs in backend code.
```

### Phase 32 — Watchdog + single-owner constraint

**Milestone:** M3 — Agents talk · **Size:** S · **Depends on:** 31

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 27 (message envelope persistence to agent_messages + Redis
Streams inbox delivery `titan:{project_id}:inbox:{agent_id}` with the requires_ack
pending-set re-entry hook), 31 (conflict-resolution state machine, escalation ladder
and security veto with persisted ConflictResolution state).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md §5
(collaboration patterns — TITAN-AGT-012/-014) and §7 (TITAN-AGT-017/-018/-020);
also §4.2 (TITAN-AGT-009 watchdog acceptance), §6.3 (requires_ack delivery
semantics) and §10 R6 (escalation storms) for the concrete behaviors below.

Build PHASE 32 — the non-LLM watchdog and the single-owner constraint:
1. The watchdog as a plain asyncio background service inside the FastAPI runtime —
   TITAN-AGT-009 forbids implementing it as another LLM agent. Four behaviors:
   (a) stalled-consumer restart: when an agent's inbox consumer stops acking, a
   replacement resumes consumption from the persisted agent_messages rows and the
   stream's pending entries, idempotent on message_id so nothing is sent twice;
   (b) unacked monitor: requires_ack messages past their type-specific timeout
   re-enter the pending set via the existing hook and trip the watchdog (§6.3);
   (c) escalation deduplication keyed on conflict_id/thread_id so repeated
   flare-ups of one dispute produce exactly one escalation, never a storm at the
   CEO/human tier (§10 R6); (d) discussion-deadline enforcement: an
   ArchitectureDiscussion thread idle past its decision deadline is auto-closed
   with a DecisionLog or escalated to the moderator's supervisor (TITAN-AGT-014).
   All timeouts and thresholds are runtime configuration that takes effect without
   redeploy (TITAN-AGT-020).
2. The single-owner constraint (TITAN-AGT-012): "every task has exactly one owner
   agent at any time" enforced in PostgreSQL itself — an Alembic migration adding
   the constraint (partial unique index on active ownership) to the existing tasks
   tables — plus a concurrency test that races two transactions assigning the same
   task to two different agents and asserts the loser fails at the DB, not in
   application code.

Extend the requires_ack pending-set hook from Phase 27 and the escalation ladder
from Phase 31 — do not build a second delivery or escalation mechanism. Add the
ownership constraint to the existing task tables via migration — no parallel
assignments table. This phase only proves consumer restart + no duplicate sends;
full plan-state resume is exercised again by the Phase 41 state machines.

Definition of done: kill the consumer mid-way through a scripted assignment
sequence — a replacement resumes within 30 seconds with zero duplicate sends,
asserted by test; concurrent assignment of one task to two agents fails at the DB.
Each watchdog behavior (restart, unacked re-entry, conflict_id/thread_id dedup,
deadline auto-close-or-escalate) has its own test. No TODOs in backend code.
```

---

## M4 — The brain

> **Milestone exit state:** Cited Memory Packets; second agent recalls first agent's learning <5s; eval gate in CI

### Phase 33 — Brain schema, Memory Events & ledger

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 6

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 6 (core PostgreSQL schema — core.projects, core.tasks —
with the Alembic migration chain; extend that chain, never fork it).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md §3
(memory taxonomy) and §4 (storage mapping; the brain schema is verbatim in §4.3),
plus requirements TITAN-BRN-001, TITAN-BRN-005, and TITAN-BRN-008.

Build PHASE 33 — the memory core: schema, Memory Events, audit ledger:
1. Alembic migrations creating schema `brain` exactly as §4.3 specifies:
   brain.memory_records (kind CHECK in episodic/semantic/procedural; scope
   'org' | 'project:<uuid>'; project_id/source_task_id FKs into core;
   source_agent; title; body_md; metadata JSONB; importance default 0.5;
   decay_score default 1.0; access_count; last_accessed_at; status CHECK in
   active/archived/superseded/deleted; superseded_by self-FK; generated STORED
   tsv column with a GIN index, plus the (scope, kind, status) index);
   brain.memory_chunks (id UUID — this id will double as the Qdrant point id;
   record_id FK ON DELETE CASCADE; seq; text; token_count; embed_model;
   embedded_at; UNIQUE(record_id, seq)); brain.ingest_outbox (chunk_id, op
   CHECK upsert/delete, enqueued_at, processed_at); and the append-only
   brain.memory_ledger (actor_agent, task_id, action CHECK in
   read/write/promote/prune/supersede, record_ids UUID[], detail JSONB).
2. Pydantic v2 Memory Event schemas (kind, scope, content_type, title, body_md,
   metadata, importance — kind and scope mandatory per TITAN-BRN-001) and
   POST /memory/events accepting a batch: for each event, insert the
   memory_record, its chunks, and the ingest_outbox rows in ONE database
   transaction — a failure anywhere rolls back everything, no partial writes.
   GET /memory/records/{id} returns the canonical record + its chunks + its
   complete ledger trail (§14 endpoint table).
3. Ledger append on every memory operation: each read, write, promote, prune,
   and supersede produces exactly one brain.memory_ledger row naming the actor
   agent, task, action, and affected record ids (TITAN-BRN-005).
4. A trivial fixed-size fallback chunker (flat token-count windows) behind a
   chunker interface keyed on the event's content_type — the four content-aware
   chunkers of §5.2 arrive next phase as drop-in implementations, so design the
   interface for that now.

Tests for every endpoint (happy path + validation failure). No TODOs in
backend code.

Definition of done: an event missing kind or scope returns HTTP 422 and the DB
CHECK constraints reject invalid enum values directly; a rollback test forces a
failure mid-batch and asserts zero record/chunk/outbox rows persisted; a test
performing N reads and N writes finds exactly 2N ledger rows (N per action).
```

### Phase 34 — Content-aware chunkers

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 33

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 33 (brain schema, Memory Event writes, and the chunker
interface with its fixed-size fallback — you are replacing that fallback for
real content types).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md §5.2
(chunking strategy matrix) and requirement TITAN-BRN-008.

Build PHASE 34 — the four production chunkers, selected by the content_type the
writer declares on each Memory Event:
1. Markdown/prose: heading-aware recursive splitter — split at ##/### headings
   first, then paragraphs; target 512 tokens with 64-token overlap.
2. Source code: syntax-aware via tree-sitter for TypeScript, Python, and SQL —
   whole functions/classes as chunk units, target 400 tokens, zero overlap
   (unit boundaries); oversize bodies split only at statement boundaries,
   never inside a function signature or DDL statement.
3. Conversations/transcripts: turn-grouped — accumulate consecutive turns until
   the 512-token budget, with 1-turn overlap between chunks.
4. Tables/schemas: never split a row or DDL statement; group rows to ~384
   tokens, repeating the header in every chunk.
5. Contextual header prefix on every chunk from every chunker, exactly
   `[{scope} | {kind} | {record title} | chunk {seq}/{n}]`, so isolated chunks
   stay interpretable to the embedder and reranker.
6. Golden-file test suites per chunker: representative fixtures (a real
   Markdown doc, TS/Python/SQL sources, a multi-turn transcript, a DDL dump)
   with expected chunk boundaries checked into the repo, so any boundary drift
   is a visible diff.

Extend the Phase-33 chunker interface — do not build a second chunking entry
point; the fallback remains only for unknown content types.

Definition of done: golden tests assert chunk sizes and overlaps match the §5.2
matrix for all four chunkers; fixtures prove zero mid-function-signature and
zero mid-DDL-statement splits (TITAN-BRN-008).
```

### Phase 35 — Qdrant collections + embedding ingestion pipeline

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 4, 10, 15, 33

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 4 (Prometheus/Grafana/Loki/OTel stack in Compose),
10 (Redis Streams QueueBus + transactional outbox machinery with idempotent
consumer groups), 15 (LLM gateway with the /embeddings route — Ollama
nomic-embed-text-v1.5 default, config-swappable), 33 (brain schema whose
Memory Event writes already populate brain.ingest_outbox).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md §4.4
(Qdrant collections), §5.1–5.4 (embedding pipeline flow and requirements),
requirements TITAN-BRN-004/006/007, and ADR-05-01 and ADR-05-02.

Build PHASE 35 — vector collections and the outbox→Streams→Qdrant pipeline:
1. Collection bootstrap for titan_memory, titan_kb, and titan_kg_labels: 768-d
   cosine named vector `dense`; indexed payload fields per the §4.4 table
   (titan_memory: scope, kind, project_id, record_id, source_agent, created_at,
   tags[], decay_score; titan_kb: doc_type, version, scope, tags[], language;
   titan_kg_labels: node_type, scope); on_disk_payload=true; HNSW m=16,
   ef_construct=128. Create collections behind aliases so a future embedding-
   model change is a background re-embed plus alias swap (ADR-05-02).
2. Ingestion pipeline per §5.1: an async outbox relay polling
   brain.ingest_outbox every 1 s and publishing to Redis Stream
   titan:memory:ingest; consumer group `embedders` (1..N workers) that request
   embeddings ONLY through the Phase-15 gateway /embeddings route, then
   idempotently upsert into Qdrant with point id == chunk id and payload built
   from the record; acknowledge only after Qdrant confirms; recover crashed
   deliveries from the Streams pending-entries list; exponential backoff with
   max 5 retries, then dead-letter to titan:memory:ingest:dlq with a Prometheus
   alert; stamp chunk.embedded_at and outbox.processed_at on success.
3. Ingestion-lag histogram exported via OpenTelemetry → Prometheus
   (TITAN-BRN-007 target: p95 ≤ 5 s from event commit to searchable at
   ≤ 10 events/s), plus the rebuild admin command (`titan memory rebuild-index`
   CLI and POST /memory/admin/rebuild-index) that fully reconstructs every
   collection from PostgreSQL, reconciling by point-id == chunk.id set
   difference (TITAN-BRN-004).

Embeddings go only via the Phase-15 gateway /embeddings — no provider SDK
imports anywhere in the Memory Service (add a grep-based test enforcing
TITAN-BRN-006). Reuse the Phase-10 outbox/Streams machinery — do not build a
second bus. No TODOs in backend code.

Definition of done: duplicate stream delivery is proven idempotent by test;
killing a worker mid-batch loses nothing (PEL recovery test); changing
EMBEDDINGS_PROVIDER in .env requires zero code edits (grep test passes);
deleting the Qdrant volume and running rebuild-index restores working search
over seeded data; the lag histogram shows p95 write→searchable ≤ 5 s under
nominal load (the recall@10 ≥ 0.99 rebuild baseline is gated in Phase 37).
```

### Phase 36 — Hybrid retrieval + reranker + Memory Packet

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 34, 35

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 34 (content-aware chunkers feeding well-formed
chunks), 35 (Qdrant collections + the ingestion pipeline that makes writes
searchable within 5 s).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md
§6.1–6.4 (retrieval stages, the Memory Packet, requirements), requirements
TITAN-BRN-003/009/010/011, and ADR-05-03.

Build PHASE 36 — the retrieval pipeline that assembles cited Memory Packets:
1. The §6.1 stages for every request (query, scope, kinds[], k, budget_tokens):
   query shaping issuing up to 3 sub-queries; parallel candidate generation —
   Qdrant dense top-40 per sub-query on titan_memory (plus titan_kb when kinds
   include procedural) with MANDATORY payload filters on scope/kind/status
   (project scope + org scope only, never other projects — TITAN-BRN-003), and
   PostgreSQL FTS top-40 on memory_records.tsv via websearch_to_tsquery +
   ts_rank_cd; Reciprocal Rank Fusion score(d) = Σ 1/(60 + rank_i(d)) with
   dedupe by record_id keeping top-24; final ordering key
   0.7·rerank_norm + 0.2·decay_score + 0.1·importance.
2. Reranking: bge-reranker-v2-m3 cross-encoder in-process via
   sentence-transformers (CPU, ONNX int8), and a FIRST-CLASS `RERANKER=off`
   fallback doing a listwise LLM rerank through the gateway's cheapest
   configured model (§15 risk R4 — both modes are supported, tested paths).
   Keep top-N chunks until 85 % of budget_tokens is filled.
3. POST /memory/packets returning the typed §6.2 packet: sections ordered
   procedural → semantic → episodic, non-evictable identity_context, a
   working_summary slot, a citation ({record_id, chunk_id, score}) for every
   included chunk, and token_cost vs budget_tokens; Redis packet cache under
   rc:{scope}:{query_hash} with TTL 300 s and a cache_hit counter
   (TITAN-BRN-012).
4. A ~30-pair query→expected-record smoke eval fixture wired into the test
   suite and run in BOTH reranker modes (the full ≥ 200-pair golden set and CI
   gate land in Phase 37).

Tests for every endpoint. No TODOs in backend code.

Definition of done: seeding two projects with distinctive markers shows zero
cross-project leakage over 100 varied queries; a query for an exact identifier
(e.g. an error code absent from dense results) is surfaced by the lexical leg;
the packet JSON validates against its schema with a citation on every chunk;
both RERANKER modes pass the smoke eval.
```

### Phase 37 — Memory eval harness + performance gate

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 36

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 36 (hybrid retrieval + reranker + POST /memory/packets
with packet cache and smoke eval fixture — this phase hardens it into gates).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md §8.2
(compression-on-retrieval — the eval must exercise packets at tight budgets),
§6.4 (TITAN-BRN-010 latency targets, TITAN-BRN-011 eval harness, TITAN-BRN-012
packet cache), §15 risk R4 (RERANKER=off is a first-class mode both gates must
cover), and the TITAN-BRN-004 acceptance in §4.5 (rebuild recall baseline).

Build PHASE 37 — the retrieval quality and performance gates:
1. A versioned golden dataset of ≥ 200 query→relevant-record pairs checked into
   the repo, covering all three kinds, both scopes, and exact-identifier
   (lexical-leg) queries; a CI job that seeds it, runs retrieval, and reports
   recall@10 and nDCG@10 against a stored baseline — any regression > 5 %
   fails the build (TITAN-BRN-011). Run the quality gate in both reranker
   modes.
2. A performance harness that generates a 100k-chunk corpus and benchmarks
   query→packet end-to-end on the local profile: p95 ≤ 800 ms with the
   reranker on, p95 ≤ 400 ms with RERANKER=off (TITAN-BRN-010); measure with
   the packet cache bypassed, then separately assert the TITAN-BRN-012 cache
   path — a second identical (scope, query) call returns < 20 ms and
   increments the cache_hit counter.
3. The rebuild-index recall gate deferred from Phase 35: capture golden-set
   results as baseline, delete the Qdrant volume, run
   `titan memory rebuild-index`, and assert recall@10 ≥ 0.99 vs baseline
   (TITAN-BRN-004 acceptance).

Definition of done: deliberately corrupting a fusion weight makes the CI eval
job fail; the 100k-corpus benchmark meets both latency targets; the delete-
volume + rebuild run passes the recall@10 ≥ 0.99 gate.
```

### Phase 38 — Working memory, context budgets & runtime memory wiring

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 29, 36

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 29 (agent runtime turn loop with tool-grant
enforcement — the loop you are wiring memory into), 36 (POST /memory/packets
hybrid retrieval returning typed, cited Memory Packets).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md §4.5
(Redis working-memory keyspace), §7 (knowledge graph — read for the §13 step-11
KG events you will stub; the KG itself is a later phase), §8.1–8.2 (token
budget slots, rolling summaries, compression-on-retrieval), §13 (the complete
memory flow around one agent invocation), and requirements TITAN-BRN-002, -009,
-015, -016, -017, -024, -025.

Build PHASE 38 — working memory and the one-packet-per-call runtime wiring:
1. Redis working memory per §4.5: wm:{task_id}:scratch (HASH),
   wm:{task_id}:summary (STRING), wm:{task_id}:events (LIST of pending Memory
   Events), all TTL 24 h. Working memory is never the sole copy of anything
   durable: the task-completion handler rejects completion while
   wm:{task_id}:events is non-empty, and flushing Redis between tasks must not
   change a replayed retrieval-only test (TITAN-BRN-002).
2. The §8.1 budget allocator: per-call budget computed from the gateway model
   registry's context window W with the slot table (system prompt 8 %/3k cap,
   identity context 6 %/2k, task instruction 8 %/3k, Memory Packet 30 %/12k,
   working summary 10 %/4k, recent verbatim turns 28 %/12k, response reserve
   10 %/8k); over-budget assembly compresses or evicts per slot rules — it
   never fails and never issues an overflowing prompt (TITAN-BRN-015).
3. Rolling summaries + compression: when verbatim history exceeds its slot,
   fold the oldest turns into wm:{task_id}:summary via a utility-tier gateway
   model (never premium — TITAN-BRN-017) using the fixed 5-field schema
   Goal / Done so far / Open threads / Constraints discovered / Next step,
   regenerated incrementally and checkpointed to PostgreSQL at least every 5
   folds (TITAN-BRN-016); compression-on-retrieval replaces over-budget
   lowest-scoring chunks with the parent record's summary line marked
   compressed:true — never silently dropped (§8.2).
4. §13 wiring into the Phase-29 runtime: every agent LLM call receives exactly
   ONE Memory Packet via POST /memory/packets — ad-hoc raw history dumps into
   prompts are prohibited (TITAN-BRN-009); write-back completes before the
   call returns: append the turn to scratch, fold the summary if needed, emit
   Memory Events (episodic decisions, semantic facts) through POST
   /memory/events with ledger rows; agent credentials cannot write org-scope
   records directly (TITAN-BRN-024); leave a typed no-op hook for §13
   step-11 KG edge events (KG lands in a later phase). Async indexing then
   makes the write searchable for the next handoff within the 5 s bound.
5. GET /memory/metrics (retrieval quality, ingestion lag, cache hits, also
   exported to Prometheus) and OpenTelemetry spans: every agent_call span
   carries its packet_id; packet-assembly spans carry per-leg candidate counts
   and rerank latency (§14).

Extend the Phase-29 turn loop — do not fork a second agent loop; all context
assembly goes through the Phase-36 packet endpoint. No TODOs in backend code.

Definition of done: a fuzz test with adversarially long tool outputs produces
zero context-overflow errors from the gateway across 1,000 calls; task
completion with unflushed working-memory events is rejected by test; a
two-agent handoff test shows the second agent retrieving what the first agent
just wrote in < 5 s; every agent_call span links to exactly one packet_id.
```

### Phase 39 — Knowledge base + knowledge graph

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 36

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 36 — hybrid RRF retrieval + reranker and typed, cited
Memory Packet assembly (POST /memory/packets), built on the brain schema + memory
ledger (33), content-aware chunkers (34), and the outbox → Redis Streams → Qdrant
embedding ingestion pipeline (35).

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md —
§7 (Knowledge Graph of Entities, ADR-05-04, TITAN-BRN-013/014), §10 (The Knowledge
Base, TITAN-BRN-021/022), and §14 (Memory Service API surface).

Build PHASE 39 — the Knowledge Base and the knowledge graph:
1. KB storage & versioning (§10.2): Alembic migration for brain.kb_documents and
   brain.kb_versions exactly as specified — slug-unique documents carrying the six
   doc_types (coding_standard, architecture_pattern, best_practice, template,
   decision_record, api_reference_note), status active|deprecated, tags[];
   versions with body_md, change_note, proposed_by, approved_by (required before
   active) and UNIQUE (document_id, version). Only the approved current version is
   chunked into the titan_kb Qdrant collection; older versions remain queryable.
2. Proposal → approval write workflow (§10.3, TITAN-BRN-021): POST /kb/proposals
   and POST /kb/proposals/{id}/approve. A proposal carries a diff + rationale; the
   service validates format, runs a semantic near-duplicate conflict check, and
   routes by type (coding_standard/decision_record need Code Reviewer approval plus
   human sign-off at MVP; other types Code Reviewer approval only). On approval:
   new kb_versions row becomes current, chunks re-embed into titan_kb, and KG
   provenance is written (kb_doc node, AUTHORED_BY, DERIVED_FROM lesson if
   applicable). Any direct mutation of KB tables via the API returns 403; every
   version retains proposer, approver, and change note.
3. Pin-by-slug (TITAN-BRN-022): extend the Memory Packet request with
   pins: ["<slug>", ...] so workflow steps can force KB documents into any packet
   independent of similarity ranking (e.g. code review always pins its
   coding_standard docs). Pinned chunks are never evicted by ranking.
4. KG schema + traversal (§7.1): brain.kg_nodes / brain.kg_edges exactly per the
   spec DDL — the 10 node types (project, component, decision, pattern, technology,
   agent, person, incident, lesson, kb_doc) and 10 edge types (PART_OF, DEPENDS_ON,
   DECIDED_FOR, SUPERSEDES, APPLIES, DERIVED_FROM, CONSTRAINS, AUTHORED_BY, USES,
   DOCUMENTED_IN), UNIQUE (scope, node_type, name), valid_from/valid_to edge
   history, indexes on (src, edge_type) and (dst, edge_type). POST /memory/kg/query
   performs typed traversal (start nodes, edge-type filter) via recursive CTE
   hard-capped at depth 3.
5. Structured-write helpers (§7.3 item 1, TITAN-BRN-013): deterministic, no-LLM
   functions callable inside a workflow transaction — approved ADR → decision node
   + DECIDED_FOR/CONSTRAINS edges; scaffolded service → component node + PART_OF;
   filed lesson → lesson node + DERIVED_FROM. Superseding a decision stamps
   valid_to and adds a SUPERSEDES edge — history is kept, never deleted.
6. Entity resolution (TITAN-BRN-014, SHOULD — cosine ≥ 0.92 merge via
   titan_kg_labels) is explicitly OUT of MVP scope for this phase: do not build a
   partial version; record the deferral in the requirements checklist. The
   titan_kg_labels collection bootstrap from Phase 35 stays as-is.

Extend the Phase 36 packet assembler and POST /memory/packets for pinning — do not
build a second packet path. Reuse the Phase 35 ingestion pipeline to embed approved
KB versions into titan_kb — no bespoke embedding code. All writes append
memory_ledger rows via the Phase 33 ledger.

Definition of done: a direct KB write attempt returns 403 while the proposal →
approve path creates a new current version with full provenance (proposer,
approver, change note) — both tested; a packet requested with
pins: ["python-service-standard"] always contains that doc's chunks, verified by
test even when the query is unrelated; a seeded fixture graph
(project → components → decisions → lessons) returns correctly from a depth-3
traversal and depth-4 paths are excluded. Tests for every new endpoint (happy path
+ 403 + validation). No TODOs in backend code.
```

### Phase 40 — Decay, curation, reflections & lessons

**Milestone:** M4 — The brain · **Size:** M · **Depends on:** 38, 39

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 38 — Redis working memory (wm:{task_id}:*), context-window
budget manager, rolling summaries, and runtime memory wiring; 39 — versioned KB with
proposal/approval workflow, pin-by-slug, and the typed knowledge graph with
structured-write helpers.

Read fully: Project-TITAN-Docs/05-Volume-5-AI-Brain-Memory-Knowledge-RAG.md —
§9 (Memory Pruning & Decay, TITAN-BRN-018/019/020), §11 (Learning From Previous
Projects, TITAN-BRN-023/024), §12 (Reflection Loops, TITAN-BRN-025/026), and §14
(curation endpoint).

Build PHASE 40 — decay/curation and the learning loops:
1. Nightly decay + curation job (§9, TITAN-BRN-018): recompute for every active
   record decay_score = 0.5·exp(−Δdays/H(kind)) + 0.3·min(1, access_count/20)
   + 0.2·importance, with half-lives episodic 30 d / semantic 120 d / procedural
   365 d and Δdays from last_accessed_at (created_at if never accessed). Reads must
   refresh last_accessed_at and increment access_count. Apply tiers exactly per
   §9.2: Demote at decay_score < 0.35 (payload flag `demoted`, excluded from
   default retrieval unless deep search), Archive at < 0.15 for 30 consecutive days
   (status='archived', Qdrant vectors deleted, PostgreSQL text retained), Compact
   for archived episodic > 180 d (body → brain.memory_bodies_cold). Runs are
   dry-run-first (plan written to the ledger); Compact/Delete above 100 records
   require Memory Agent approval; every transition appends a memory_ledger row.
   Expose POST /memory/admin/curation/run plus the nightly schedule.
2. Protected classes (TITAN-BRN-019): decision records, lessons, KB documents, and
   anything referenced by an active KG edge from an active project are never
   auto-pruned, even at decay_score = 0. Auto-restore (TITAN-BRN-020): accessing a
   demoted/archived record flips it active, re-embeds its chunks into Qdrant within
   the 5 s ingestion budget, and resets its decay clock.
3. Lessons-learned records (§11.2): structured lesson schema (episodic record,
   record_subtype 'lesson', with title, context, observation, root_cause,
   recommendation, evidence[], applicability, importance), written for every
   incident, systemic review rejection, failed deployment, or retrospective
   finding, with a lesson KG node + DERIVED_FROM edge via the Phase 39 helpers.
4. Pattern miner (§11.1, TITAN-BRN-023): milestone-triggered job that collects
   decision records, the component graph, and code-review outcomes; an LLM pass
   (utility tier through the gateway; mock provider in tests) proposes candidate
   patterns; novelty-check against org memory at 0.85 similarity; novel candidates
   become project-scope pattern records with APPLIES/DERIVED_FROM evidence edges.
   Miner runs are visible in the ledger.
5. Project → org promotion (§11.3, TITAN-BRN-024): promote a lesson/pattern only on
   the multi-signal rule — observed in ≥ 2 projects (cross-scope near-duplicate
   detection), or flagged org-candidate and approved in curation review, or marked
   by a human. Promotion clones the record into org scope with provenance edges
   ('promote' ledger action); procedural content triggers a KB proposal through the
   Phase 39 workflow. The API rejects direct org-scope writes from agent
   credentials.
6. Reflection APIs (§12, TITAN-BRN-025): task reflection — the fixed five-question
   rubric (goal / what worked / what surprised / do differently / what should the
   org remember) in ≤ 250 tokens on the utility model tier, persisted as an
   episodic record that may spawn a lesson or KB proposal; expose a done-block hook
   so the task state machine (wired in Volume 6 phases) can refuse the `done`
   transition without a linked, schema-valid reflection. Sprint retrospective API:
   aggregates task reflections, review statistics, and blocker history into the
   sprint summary plus 0–3 lessons.

Route all miner/reflection LLM calls through the existing gateway at the cheapest
configured tier (TITAN-BRN-017) — never premium models. Reuse the Phase 39 KB
proposal workflow and KG structured-write helpers, the Phase 35 ingestion pipeline
for re-embeds, and the Phase 33 memory_ledger — do not build parallel mechanisms.

Definition of done: a test seeding records with back-dated timestamps and access
counts runs the curation job (dry-run plan, then apply) and gets exactly the
expected Demote/Archive/Compact transitions with matching ledger rows, while
protected records at decay_score = 0 are untouched; an integration test posts a
task reflection, sees it land as an episodic memory record, and retrieves it in the
next Memory Packet; a direct org-scope write with agent credentials is rejected by
test. Tests for every new endpoint and the done-block hook. No TODOs in backend
code.
```

---

## M5 — Autonomous delivery

> **Milestone exit state:** Mock-LLM brief runs all 12 handoffs; full 16-agent todo-app completes offline on Ollama

### Phase 41 — Pipeline & task state machines

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 14, 17, 24, 29

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 14 (worker process & run lifecycle), 17 (gateway cost
accounting, budget caps and the resumable budget_hold flag), 24 (WebSocket realtime
backend with event fan-out consumers), 29 (agent runtime turn loop) — plus everything
earlier, notably the Phase-13 task transition table + race-safe scheduler and the
Phase-10 transactional outbox on Redis Streams.

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§2.1–2.2 (pipeline overview & handoff contract; TITAN-WFL-001/002) and §2.5 (task
state machine; TITAN-WFL-009).

Build PHASE 41 — pipeline & task state machines:
1. Per-project pipeline state machine over the 12 stages of §2.1 (CEO vision → PM
   backlog → Project Manager plan → Architect design → Engineering → QA → Security →
   Code Review → DevOps deploy → Documentation → CEO release approval → handover),
   persisted in PostgreSQL with current stage + iteration count per TITAN-WFL-001.
   Every stage transition is written through the transactional outbox and emitted on
   Redis Streams as pipeline.<project>.stage_complete, with WebSocket fan-out to the
   dashboard via the Phase-24 consumers.
2. No-skip enforcement per TITAN-WFL-002: stages may never be skipped or reordered;
   an out-of-order transition raises a rejected-transition event and leaves state
   unchanged. The single exception is a CEO-Agent NOT_APPLICABLE marking with a
   logged justification (e.g., a docs-only change skipping DB engineering).
3. Task state machine BACKLOG → READY → IN_PROGRESS → IN_LOOP → MERGED → DONE, plus
   BLOCKED, FAILED, ESCALATED, and REOPENED → READY, exactly along the §2.5 edges.
   DONE requires the parent story's acceptance criteria to pass; every state change
   emits an event consumed by the Kanban artifact (TITAN-WFL-009).
4. budget_hold: wire the Phase-17 budget flag into both machines — at 100% budget a
   task pauses in the resumable budget_hold state, no further LLM calls are made for
   it, and raising the budget resumes it exactly where it paused.

Extend the Phase-13 transition table and scheduler — one task machine only, with the
Volume-6 states mapped onto it; do not build a second transition mechanism. Reuse the
Phase-10 outbox and Phase-24 WebSocket consumers for all events. No TODOs in backend
code; unit tests for every legal and illegal transition.

Definition of done: killing and restarting the orchestrator mid-pipeline resumes the
project at the exact stage and iteration count, and the event log reconstructs the
full history; illegal task transitions (e.g., BACKLOG → MERGED) are rejected and
logged, and Kanban events are observed on the WebSocket during a scripted run;
killing the orchestrator mid-plan has a replacement resume within 30 seconds with no
duplicate task assignments.
```

### Phase 42 — Handoff executor, gate ledger & 12 artifact schemas

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 41

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 41 (per-project 12-stage pipeline state machine and the
Volume-6 task state machine, all transitions through the outbox with WebSocket
fan-out).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§2.2 (the handoff contract 4-tuple), §2.3 (handoff specifications H1–H12), §2.5, and
requirements TITAN-WFL-003/004/005.

Build PHASE 42 — handoff executor, gate ledger & 12 artifact schemas:
1. Generic handoff executor implementing the §2.2 4-tuple: trigger (producer stage
   marks COMPLETE via pipeline.<project>.stage_complete on Redis Streams) → versioned
   Pydantic-v2-validated artifact stored in the artifacts table (and embedded per the
   contract) → acceptance-gate evaluation → bounded failure path. Schema-validation
   failure counts as gate failure and attaches a field-level error report to the
   failure event (TITAN-WFL-003). Concrete gate predicates G1–G12 land in later
   phases; here build the framework plus a configurable fake gate for tests.
2. Gate ledger per TITAN-WFL-004: every gate evaluation persisted with verdict,
   evaluator identity (agent + model + prompt version), reasons, and timestamps,
   exportable as a complete pass/fail ledger for any release. Retry budget per
   TITAN-WFL-005 (default 3 per stage, configurable per project): on exhaustion,
   pause the pipeline, mark the project NEEDS_HUMAN, and notify the user with a
   structured escalation report (what failed, all attempts, agent hypotheses).
3. All 12 handoff artifact schemas from §2.3, fully fielded and versioned:
   UserRequest, ProjectBrief (vision ≤300 words, MoSCoW feature list, non-goals,
   constraints, success criteria), ProductBacklog (INVEST stories with Gherkin
   acceptance criteria, priority, points), SprintPlan + TaskGraph (tasks ≤1 ideal
   agent-day, DAG dependencies, sprint/milestone mapping), ArchitecturePackage,
   MergeCandidateManifest, QAReport, SecurityClearance, ReleaseReview,
   DeploymentRecord, DocBundle, ReleaseApproval (with traceability table).

Drive the executor off the Phase-41 pipeline machine — handoffs ARE its stage
transitions, not a parallel workflow engine. No TODOs in backend code; tests for
every schema and every executor path.

Definition of done: an artifact missing a required field is rejected with a
field-level error report attached to the failure event; a stage forced to fail 4
times produces exactly one escalation, the project lands in NEEDS_HUMAN, and zero
further LLM calls occur for that stage; all 12 schemas round-trip representative
example instances built from the §2.3 field lists (construct → serialize → re-parse
identical).
```

### Phase 43 — Gates G1–G5 + constitution document + CEO clarification engine

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 42

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 42 (generic handoff executor, gate ledger with evaluator
identity, retry budgets → NEEDS_HUMAN escalation, all 12 artifact schemas).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§2.3 (H1–H5), §2.4 (CEO clarification policy; TITAN-WFL-007/008), §6 (the engineering
standards constitution), and TITAN-WFL-005.

Build PHASE 43 — gates G1–G5, constitution document & CEO clarification engine:
1. Gate implementations plugged into the Phase-42 executor, each with pass/fail
   fixtures and the §2.3 failure-path routing: G1 (UserRequest non-empty and not a
   duplicate of an active project), G2 (every MUST-feature has a measurable success
   criterion; non-goals non-empty; constraints include the canonical stack unless
   user-overridden; failures return the brief to the CEO with gap annotations), G3
   (every story INVEST-checked by rubric prompt + lint rules — has actor, has
   outcome, ≤13 points, ≥1 testable Gherkin AC — and every MUST-feature covered by
   ≥1 story; partial acceptance allowed at G3 only), G4 (task DAG acyclic, every
   story maps to ≥1 task, sprint 1 contains a walking-skeleton slice, no sprint over
   capacity; failures return the offending subgraph to the Project Manager), G5
   (ArchitecturePackage complies with the constitution via checklist evaluation,
   every TaskGraph task references a package component, API contracts validate as
   OpenAPI 3.1; failures return numbered objections to the Architect).
2. The engineering standards constitution (Volume 6 §6, verbatim) as a versioned
   agent-context data artifact — retrieved into system context, never hardcoded into
   prompts — so G5 and reviewers always evaluate against a specific committed
   version. Machine enforcement (the CI kit) lands in Phase 45; here build only the
   artifact, its versioning, and its retrieval into context.
3. CEO clarification engine per TITAN-WFL-007/008: at most 5 questions per project,
   in a single batch, asked only when the answer is not inferable from the request or
   organization memory, not covered by a canonical default, and materially changes
   the ProjectBrief; every other unknown becomes a logged, user-overridable
   assumption in the brief's assumptions section; a configurable non-blocking timeout
   (default 24h) proceeds with best-judgment assumptions, which the H12
   ReleaseApproval must restate.

No TODOs in backend code; every gate has both pass and fail fixture tests.

Definition of done: every G1–G5 fixture passes or fails deterministically and routes
failures to its §2.3 failure path (e.g., a G4 cycle returns the offending subgraph);
each G5 ledger entry records the constitution artifact version it judged against; a
clarification batch is persisted with its questions and assumptions, and an
unanswered batch proceeds after timeout with all assumption-driven decisions flagged.
```

### Phase 44 — Self-improvement loop engine + reviewer isolation

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 31, 43

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 31 (conflict-resolution state machine with escalation
ladder and security veto), 43 (gates G1–G5, versioned constitution artifact,
clarification engine).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§3 (self-improvement loop: §3.1 overview, §3.2 stage specifications, §3.3 iteration
bounds & escalation; TITAN-WFL-010 through 014).

Build PHASE 44 — self-improvement loop engine & reviewer isolation:
1. The 9-stage per-task loop per §3.1–3.2: Implementation → Review → Critique →
   Refactor → Testing → Security Scan → Perf Analysis → Approval → Merge, with each
   stage's actor, output artifact, and pass condition from the §3.2 table (e.g.,
   Testing requires ≥80% line coverage on changed code and a covering test for every
   fixed finding; Perf checks p95 endpoint budgets and N+1 queries). A persisted loop
   record is mandatory before a task may reach MERGED (TITAN-WFL-010 — no first
   drafts, enforced as an orchestrator gate on the Phase-41 task machine). Hard cap
   of 3 full cycles (configurable 2–5); convergence early-exit on two consecutive
   cycles with zero new findings of severity ≥ minor, logged distinctly from
   exhaustion; exhaustion sets the task ESCALATED for Architect review — re-scope,
   override findings with a logged ADR, or escalate to the human (TITAN-WFL-013).
2. Adversarial reviewer isolation per TITAN-WFL-011: the reviewing agent instance
   shares no conversation context with the implementing instance — it receives only
   the PR diff, the task spec, the ArchitecturePackage excerpt, and the Phase-43
   constitution artifact. Refute-or-fix per TITAN-WFL-012: every critique finding
   gets an explicit fixed/refuted disposition, refutations are re-judged by the
   reviewer, and a finding upheld and refuted twice routes to the Architect for
   binding arbitration. Add the oscillation short-circuit (TITAN-WFL-014): an A→B→A
   edit sequence on the same hunk triggers arbitration within the same cycle.

Route arbitration and escalation through the Phase-31 conflict-resolution state
machine — do not build a second escalation ladder. IN_LOOP on the Phase-41 task
machine is the loop's only entry point. Test with a mock LLM; no TODOs.

Definition of done: a converging task exits after 2 cycles with a distinctly logged
convergence record; a non-converging task records exactly 3 cycles then ESCALATED —
no task ever records a 4th cycle; a task with no loop record cannot transition to
MERGED; a twice-refuted upheld finding produces an Architect arbitration request; the
reviewer's message log contains no implementation-side chain-of-thought.
```

### Phase 45 — Constitution CI enforcement kit

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 21, 43

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 21 (git, terminal and code tools operating in sandboxed
workspaces), 43 (versioned constitution artifact delivered into agent context).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§6.1–6.5 (architecture standards, naming conventions, error handling, logging,
testing gates; TITAN-WFL-022 through 028, and the TITAN-WFL-030 coverage gates).

Build PHASE 45 — constitution CI enforcement kit for generated workspaces:
1. A CI kit that TITAN installs into every generated project workspace, enforcing
   the machine-checkable constitution subset: import-linter (Python) and
   eslint-boundaries (TypeScript) rules for Clean Architecture layering — domain ←
   application ← infrastructure ← interface, dependencies pointing inward only, so a
   domain → infrastructure import fails CI (TITAN-WFL-022); the grep gate failing any
   sqlalchemy import outside infrastructure (TITAN-WFL-025); Ruff + ESLint + a SQL
   migration linter + a Conventional Commits commit-message hook implementing the
   §6.2 naming table (TITAN-WFL-027); diff coverage gates of ≥80% line / ≥70% branch
   on changed code, never decreasing project-wide (TITAN-WFL-030).
2. One-command installation (a single init script/CLI the DevOps agent can run via
   the Phase-21 terminal tool) that drops all configs, hooks and the CI workflow into
   a fresh workspace.
3. Fixture workspaces, one per machine rule, each violating exactly one rule (a
   domain → infrastructure import, sqlalchemy in a router, a camelCase Python
   function, a non-conventional commit message, an under-covered diff), used as the
   kit's own regression tests.

This kit is the machine-enforcement layer of the Phase-43 constitution artifact —
keep every rule traceable to its TITAN-WFL requirement id; do not fork a second
constitution. No TODOs.

Definition of done: each fixture workspace fails CI at exactly the gate its violation
targets while passing all other gates; the kit installs into a fresh workspace with
one command, and a clean workspace passes every gate.
```

### Phase 46 — Generated-workspace templates: errors, logging, test pyramid

**Milestone:** M5 — Autonomous delivery · **Size:** S · **Depends on:** 45

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 45 (constitution CI enforcement kit, installable into a
generated workspace with one command).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md —
§6.3–6.7 (error handling, logging, testing strategy & coverage gates, review rules,
documentation; TITAN-WFL-028 through 031 and 034).

Build PHASE 46 — generated-workspace templates for errors, logging and testing:
1. Error-handling template per TITAN-WFL-028: a DomainError hierarchy (NotFoundError,
   ConflictError, ValidationError, AuthorizationError) raised from domain/application
   layers and translated exactly once, at the interface layer, into RFC 9457
   problem-details responses with stable machine-readable type codes; no bare except,
   no swallowed exceptions, no client-visible stack traces or internal identifiers,
   external I/O wrapped with timeouts and typed failure mapping, backoff+jitter
   retries on idempotent operations only — plus the verification suite asserting
   every endpoint returns problem-details on induced failure while logs retain the
   stack trace the client response omits.
2. Structured-logging template per TITAN-WFL-029: structlog/pino-equivalent JSON with
   mandatory fields timestamp, level, service, trace_id/span_id, project_id,
   agent_id, event; the DEBUG/INFO/WARNING/ERROR/CRITICAL level policy; and the
   redaction filter + CI log-call scanner keeping secrets, tokens and PII out of
   logs.
3. Test-pyramid scaffolding per TITAN-WFL-030/031: pytest/Vitest unit layer,
   testcontainers integration layer against real PostgreSQL 16 and Redis 7,
   Playwright e2e slot for critical journeys, k6 performance slot for budgeted
   endpoints — plus the hermeticity policy (no calls to real external services,
   injected clocks and seeds, parallel-safe) enforced by a template test gate.

Templates render into generated workspaces alongside the Phase-45 kit and must pass
that kit unmodified — never relax a Phase-45 gate to make a template pass.

Definition of done: rendering the templates into a fresh workspace produces a project
that passes the full Phase-45 CI kit including the templates' own error, logging and
test suites; a fixture containing a hermeticity violation (a test hitting a real
external URL, or unseeded randomness) fails the template test gate.
```

### Phase 47 — Reviewer R1–R11 rules + fixtures + doc/quickstart checkers

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 22, 44, 45, 46

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 22 (docker & browser tools: docker.build/compose_up/
compose_down/logs and Playwright browser.* in sandboxed containers), 44 (self-
improvement loop engine with isolated reviewer instance and loop records), 45
(constitution CI enforcement kit: import boundaries, naming, coverage, secret
scan), 46 (generated-workspace templates for errors, logging, and the test
pyramid).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md
§6.1 through §6.7 (the Engineering Standards Constitution — especially the
§6.6 R1–R11 rejection table and §6.7 documentation requirements), plus
TITAN-WFL-012/013 in §3.2–3.3 and TITAN-WFL-032/033/034.

Build PHASE 47 — the Code Reviewer rubric, its regression fixtures, and the
documentation checkers:
1. Encode the §6.6 rejection table as typed rules R1–R11 (Pydantic models, not
   prompt-only): R1 Clean-Architecture/SOLID violation, R2 missing/failing
   tests or diff coverage below the §6.5 gates, R3 high/critical security
   finding or hardcoded secret or missing input validation, R4 duplicated
   logic >10 similar lines where an abstraction exists, R5 swallowed errors/
   bare excepts/client-visible stack traces, R6 CQRS or event-sourcing without
   a linked ADR, R7 placeholder code (TODO/FIXME, stubs, commented-out code),
   R8 public API change without OpenAPI update + versioning, R9 performance
   budget exceeded or N+1 introduced, R10 naming/lint violations that bypassed
   CI, R11 missing docstrings/JSDoc or missing migration rollback. Severities:
   R1–R9 blocking, R10–R11 major; two majors equal one blocking condition.
   Verdict set: APPROVED, APPROVED_WITH_COMMENTS, REJECTED (TITAN-WFL-032).
2. Enforce finding discipline per TITAN-WFL-033: every finding carries file,
   line, violated rule ID, and a concrete suggested fix; findings outside the
   diff's blast radius are marked `advisory` and can never block. Wire refute-
   or-fix per TITAN-WFL-012: every finding ends `fixed` or `refuted`,
   refutations are re-judged, and a finding refuted twice escalates to the
   Architect — extend the Phase-44 loop records, do not invent a second
   disposition store.
3. Prompt-regression fixture PRs: one fixture PR per rule R1–R11 (e.g. a
   domain→infrastructure import for R1, an 11-line copy-paste for R4, a TODO
   stub for R7, an endpoint change with stale OpenAPI for R8) plus at least
   two clean PRs, stored as repo fixtures and runnable as a suite against the
   Phase-44 reviewer with the mock provider.
4. Doc coverage checker implementing the machine-checkable half of §6.7 /
   TITAN-WFL-034: verifies every public endpoint appears in the API reference
   with prose, every ADR is indexed, and the README contains a Docker Compose
   quickstart; emits a structured coverage report (this becomes gate G11's
   input in a later phase).
5. Quickstart executor: parses the README quickstart commands and executes
   them in a clean Docker Compose environment using the Phase-22 docker tools
   (docker.compose_up, docker.logs, health probe), failing on non-zero exit,
   unhealthy services, or an unreachable app URL.

Extend the Phase-44 reviewer and loop engine and the Phase-22 docker tools —
do not build a second review pipeline or a second compose runner.

Definition of done: the reviewer rejects each R1–R11 fixture PR citing the
correct rule code and approves the clean fixtures; 100% of findings in the
fixture runs carry file/line/rule citations; a deliberately broken quickstart
fixture fails the executor while a working one passes. Tests for every rule
and both checkers. No TODOs in backend code.
```

### Phase 48 — Git workspaces + local Compose deploy

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 21, 22, 23

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 21 (git/terminal/code tools: git.status/diff/branch/
commit/merge, argv-only terminal.run with streamed output, code.lint/test),
22 (docker tools via rootless sidecar daemon per workspace: build, compose_up,
compose_down, logs, ps), 23 (web tools + local registry mirror + egress proxy).

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
requirements TITAN-FND-012 and TITAN-FND-027 (§4 requirement tables), and
Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md §6.7 with
TITAN-INT-046.

Build PHASE 48 — per-project versioned workspaces and real local deployment of
generated apps:
1. Per-project git workspace per TITAN-FND-012: every TITAN project gets its
   own git repository inside its sandboxed workspace, and pipeline artifacts
   (frontend, backend, database, infrastructure, tests, docs, deployment
   scripts, config) land as agent-authored commits through the Phase-21 git
   tools — Conventional Commit messages and `type/task-id-slug` branches per
   Volume 6 §6.2, agent identity recorded as the commit author.
2. Generated apps are self-contained: each generated project ships its own
   docker-compose.yml and builds and runs by itself via the Phase-22 docker
   tools (docker.build, docker.compose_up) against the workspace's rootless
   sidecar daemon, pulling images through the Phase-23 registry mirror so it
   works with no external accounts.
3. The `titan.local` connector exposing the `deploy.compose` tool (Volume 7
   §6.7 table, TITAN-INT-046): deploying a generated app allocates a local
   port, brings the app's compose stack up, health-checks it, records the
   reachable URL on the deployment record, and streams the deployment and
   container logs to the dashboard Logs view over the existing tool.output
   WebSocket path. Include compose-down/teardown and re-deploy idempotency.
   Per TITAN-FND-027 and TITAN-INT-046 this is the default deploy target and
   must require zero cloud credentials.

Definition of done: a demo generated app deploys locally with no cloud creds
configured and its URL responds while logs stream to the UI; `git log` in the
project workspace shows agent-authored commits on task branches; deploy,
teardown, and re-deploy are integration-tested. No TODOs in backend code.
```

### Phase 49 — Gates G6–G12 + traceability + debt ledger

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 47, 48

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 47 (typed R1–R11 reviewer rubric with verdict set,
doc coverage checker, quickstart executor), 48 (per-project git workspaces and
real `titan.local deploy.compose` local deployment with streamed logs). The
gate evaluator, gate ledger, and 4-tuple handoff contract (trigger, artifact,
gate, failure path) already exist from gates G1–G5.

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md
§2.3 handoffs H6–H12, plus TITAN-WFL-004/005/006 and TITAN-WFL-021.

Build PHASE 49 — the execution-side gates G6–G12, the traceability table, and
the technical-debt ledger:
1. G6 (H6, Engineers → QA): CI green on the integration branch (lint,
   typecheck, unit + integration suites, §6.5 coverage gates) plus the dual
   migration check — migrations apply cleanly to a fresh PostgreSQL 16 AND to
   a snapshot of the previous schema. Failure reopens the failing components
   as tasks re-entering the self-improvement loop at Refactor with the CI
   failure attached as critique input.
2. G7 (H7, QA → Security): QAReport artifact with test matrix, defects
   classified P0–P3, flake log; gate requires zero P0/P1, e2e critical-path
   suite (run via the phase-22 browser.* Playwright tools) 100% passing, and
   regression passing. P0/P1 defects become tasks routed to the owning
   engineer role; retry budget applies to full QA cycles.
3. G8 (H8): SecurityClearance — zero high/critical findings; mediums require
   an accepted-risk entry approved by the CEO agent or a remediation task
   scheduled within the release; a repeated identical finding escalates
   immediately without consuming the full retry budget.
4. G9 (H9): ReleaseReview — run the Phase-47 reviewer at release scope over
   the integrated diff (architecture boundaries, duplication, public-API
   consistency, changelog accuracy). REJECTED produces itemized rework
   tickets; APPROVED_WITH_COMMENTS proceeds but logs debt entries.
5. G10 (H10): DeploymentRecord — staging smoke suite green and health checks
   green through the soak window (default 15 min, configurable down for
   tests), traces flowing. Failure triggers automatic rollback to the
   rollback pointer, an incident event, a defect task, and pipeline return to
   H6 scope.
6. G11 (H11): DocBundle gated by the Phase-47 doc coverage checker and
   quickstart executor; a failing quickstart is a P1 defect routed to H7, not
   a docs issue.
7. G12 (H12): ReleaseApproval — every MUST-feature traced to passing
   acceptance tests; scope changes require user confirmation, never silent
   narrowing.
8. Traceability table generator per TITAN-WFL-006: brief feature → stories →
   tasks → PRs → tests → docs, generated automatically per release,
   exportable, with gap detection.
9. Debt ledger per TITAN-WFL-021: every APPROVED_WITH_COMMENTS verdict and
   accepted constitution deviation creates a technical-debt ticket with a
   paydown task proposed into a future sprint; the ledger total is exposed
   via API for the project dashboard.

G10 and G11 exercise the phase-48 real deploy.compose path and the phase-22
browser/docker tools — no stubs or simulated deploys. Extend the existing
G1–G5 gate evaluator and gate ledger — do not build a second gate mechanism.

Definition of done: fixture scenarios for each of G6–G12 pass and fail with
the correct failure-path routing, persisted with verdict, evaluator identity,
and reasons per TITAN-WFL-004; an induced G10 failure against a real local
deploy triggers rollback + incident event + reopen at H6 (use a shortened
soak window); a test proves every APPROVED_WITH_COMMENTS verdict has a
corresponding debt ticket. No TODOs in backend code.
```

### Phase 50 — Sprint planner, ceremonies, milestones & blocker ladder

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 43

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 43 (gates G1–G5 with the 4-tuple handoff contract,
the constitution document wired into agent context, and the CEO clarification
engine with the 5-question batch policy).

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md
§2.4 and §4, plus TITAN-WFL-008 and TITAN-WFL-015/016/017/018/019.

Build PHASE 50 — autonomous planning mechanics:
1. Auto sprint planner per TITAN-WFL-015: partition the TaskGraph into sprints
   such that sprint 1 is a walking skeleton (thin end-to-end slice with ≥1
   task per engineering role plus a deploy task), no task lands in an earlier
   sprint than any of its dependencies, sprint size respects configured
   parallel-agent capacity, and each sprint ends with a potentially
   releasable increment.
2. Sprint ceremonies as agent events per TITAN-WFL-016: an automated planning
   record (goals, committed stories, risks) at sprint open, and review (demo
   links vs. acceptance criteria, velocity actuals vs. estimates) plus
   retrospective records at sprint close, persisted and fed to organization
   memory. Implement TITAN-WFL-017 estimation feedback: recalibrate story
   points from actuals (tokens, wall-clock, loop cycles) with the correction
   factor stored in organization memory.
3. Milestone tracking per the §4.2 table (M0 Inception, M1 Walking Skeleton,
   M2 Feature Complete, M3 Hardened, M4 Released, each with its definition of
   done and gate owner) and TITAN-WFL-018: milestones strictly ordered;
   milestone state always equals the highest milestone whose definition of
   done currently holds; a regression (e.g. new P0 after M2) reverts the
   project to the earlier milestone and reopens the relevant pipeline stage.
4. Blocker detection + recovery ladder per TITAN-WFL-019: flag a task BLOCKED
   when any of — no state change beyond 2× its estimate, dependency failed,
   waiting on an unanswered clarification, loop escalation pending. Recovery
   runs strictly in order: dependency re-check → reassignment to a fresh
   agent instance with summarized context → decomposition into smaller tasks
   → human escalation, on a scheduler tick of ≤60 s.
5. Non-blocking clarification per TITAN-WFL-008 (§2.4): extend the Phase-43
   clarification engine — if the user does not answer within the configurable
   window (default 24h), the CEO proceeds with best-judgment answers recorded
   as high-visibility assumptions, restated at release approval (H12). Do not
   build a second clarification mechanism.

Definition of done: a fixture backlog yields a walking-skeleton-first sprint
plan where no task precedes its dependencies and sprint 1 contains per-role
plus deploy tasks; an induced regression (new P0 after M2) reverts the
milestone and reopens the stage; a synthetically stalled task is detected and
recovery attempted within one ≤60 s scheduler tick (tested with an injected
clock); every completed fixture sprint has all three ceremony records
persisted. No TODOs in backend code.
```

### Phase 51 — Derived artifacts + event replay + mock-LLM E2E

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 40, 49, 50

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 40 (memory decay, curation, reflections and lessons-
learned storage), 49 (gates G6–G12, traceability table generator, debt
ledger), 50 (sprint planner, ceremonies, milestone tracking, blocker ladder).
All twelve handoffs H1–H12 and both loops now exist end to end.

Read fully: Project-TITAN-Docs/06-Volume-6-Engineering-Workflow-and-Standards.md
§5, plus TITAN-WFL-018/019/020/021.

Build PHASE 51 — the six derived project artifacts and the full-pipeline proof:
1. The six auto-generated artifacts from the §5 table, each a derived view
   over the PostgreSQL event log — never hand-maintained: Roadmap (milestones
   + sprint plan + brief priorities; regenerated on any milestone/sprint
   change; Timeline JSON + Markdown), Kanban board data (task state-machine
   events; every task transition), Dependency graph (TaskGraph DAG +
   component graph; task/architecture change; DOT + rendered view), Release
   plan (milestones + DeploymentRecords + traceability table; gate G6–G12
   events; Markdown), Risk analysis (escalations, blocker history, security
   mediums, assumption log; sprint close + any escalation; ranked register
   with likelihood × impact, owner, mitigation), Technical debt report
   (APPROVED_WITH_COMMENTS items, deferred findings, coverage deltas, debt
   tickets; sprint close; ranked ledger with paydown tasks).
2. Per TITAN-WFL-020: each artifact regenerates automatically on its trigger
   conditions, is versioned per regeneration, and is exportable as Markdown
   and JSON via API endpoints. Regeneration must be fully deterministic
   (stable ordering, no wall-clock timestamps in content) so replay
   reproduces byte-identical output.
3. End-to-end mock-LLM test: drive a tiny project brief through all twelve
   handoffs H1–H12 to a simulated release — clarification/assumptions, sprint
   planning, self-improvement loop cycles, gates G1–G12 with correct
   failure-path routing on at least one induced failure, milestone
   progression M0→M4, debt ledger entries, and reflection/retrospective
   records landing in memory (Phase 40).

Definition of done: a test deletes all artifact rows, replays the event log,
and asserts byte-identical current versions of all six artifacts; the E2E
mock-LLM run completes with every handoff, gate verdict, and loop iteration
queryable in the database via the export APIs. No TODOs in backend code.
```

### Phase 52 — Plugin runtime: manifest, lifecycle, sandbox, webhooks, UI panels

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 19, 20

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 19 (Tool Bus risk classes read/write/admin, approval
gate, validation and audit pipeline), 20 (workspace sandbox with jailed fs
tools and mount policy).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§3.2–3.5 and §7, plus TITAN-INT-010 through TITAN-INT-017, TITAN-INT-011/050,
and ADR-INT-01/ADR-INT-02.

Build PHASE 52 — the manifest-driven plugin runtime:
1. Plugin manifest per §3.2: `titan-plugin.yaml` validated with Pydantic v2
   and published as JSON Schema at schemas/plugin-manifest.v1.json — id
   (reverse-DNS), SemVer version, titan_api compatibility range, runtime
   (type python|mcp, entrypoint), permissions (network egress allowlist,
   named secrets, event subscribe/publish topics, RBAC scopes, filesystem
   globs), contributes (tools with schema + risk, agents, ui_panels,
   webhooks), config_schema. Installation fails with actionable field-level
   errors on schema violation, unknown permission scope, or titan_api
   incompatibility (TITAN-INT-010).
2. Lifecycle per §3.3 as REST operations on /api/v1/plugins: AVAILABLE →
   INSTALLED (validate, resolve deps into an isolated per-plugin virtualenv —
   never the core environment — persist a plugin_installs row with pinned
   version + content hash) → ENABLED (operator with RBAC role plugin:admin
   grants the declared permissions; healthcheck must return OK within 10 s) →
   QUARANTINED / REMOVED. Transitions are atomic and crash-safe
   (TITAN-INT-013); disable revokes permissions and unloads tools atomically
   (in-flight calls drain, new calls rejected with TOOL_UNAVAILABLE); an
   upgrade that widens permissions re-enters approval before activation
   (TITAN-INT-017).
3. Out-of-process sandbox runtime per §3.5 and ADR-INT-01: plugins run in a
   per-plugin subprocess (`python -I`, resource limits) speaking JSON-RPC 2.0
   over stdio, with framing deliberately identical to MCP (§7, ADR-INT-02) so
   the MCP client built in the next phase shares this codebase — keep the
   transport layer abstracted, but do not implement MCP server connections
   yet. A crashing (`os._exit(1)`) or hanging plugin must not take down core
   (default 60 s call timeout, TITAN-INT-016); three consecutive unhandled
   exceptions or a failed healthcheck auto-quarantines within 30 s,
   unregistering tools and emitting `plugin.quarantined` on Redis Streams
   (TITAN-INT-014).
4. Permission model per §3.4: deny-by-default capability grants checked at
   grant time AND call time — the Tool Bus verifies the calling agent's RBAC
   scopes intersect the tool's required scopes, so a plugin can never launder
   an agent into privileges it lacks. All five classes enforced (network,
   secrets, scopes, events, filesystem); any undeclared/ungranted capability
   touch fails closed with a PERMISSION_DENIED audit event (TITAN-INT-015).
   Plugin-contributed tools register into the Tool Registry and route through
   the Phase-19 Tool Bus gating and audit — do not build a parallel dispatch
   path.
5. Webhook router: manifest `webhooks` entries mount at
   /api/v1/plugins/<plugin_id>/hooks/<path> with HMAC-SHA256 signature
   verification against the named secret; unverified payloads are rejected
   and logged.
6. UI panel contributions: `contributes.ui_panels` (types iframe |
   schema-form | markdown) mounted at named dashboard extension slots, static
   assets served at /plugin-ui/<plugin_id>/...; the config_schema renders as
   a settings form with values stored encrypted and secrets referenced by
   name only (TITAN-INT-011 — zero core modifications per plugin).

Definition of done: a sample JSON-RPC plugin installs, enables, and
contributes a tool via manifest only, with its calls gated and audit-logged
by the Tool Bus; a conformance test attempts one violation per permission
class and all fail closed with PERMISSION_DENIED, and a sandbox violation
moves the plugin to QUARANTINED with its tools unregistered; the sample
plugin's UI panel serves at /plugin-ui/<id>/ with no core code changes; crash
and hang tests leave the orchestrator serving requests. Tests for every
lifecycle transition and rejection class. No TODOs in backend code.
```

### Phase 53 — MCP client

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 52

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 52 — the plugin runtime (manifest schema, enable/disable
lifecycle, per-plugin sandboxed out-of-process execution speaking JSON-RPC over
stdio, webhook endpoints, UI panels).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§7 (MCP — Model Context Protocol Support), requirement TITAN-INT-050, and
ADR-INT-02 (MCP as first-class transport), plus §3.5 (plugin sandboxing) and
§4.2 (Tool Bus invocation flow) for the pipelines you must plug into.

Build PHASE 53 — the MCP client:
1. Extend the Phase-52 plugin manifest with `runtime.type: mcp` and
   `transport: stdio | http`: stdio servers declared via
   `command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", ...]` and
   Streamable HTTP servers via `url:`. All other manifest sections (permissions,
   scopes, risk overrides, egress allowlist) apply identically — an MCP server is
   an ordinary plugin. stdio servers run under the same per-plugin sandbox regime
   as Python plugins (§3.5); HTTP servers are bound by the manifest egress
   allowlist.
2. Discovery: on plugin enable, perform MCP `initialize` then `tools/list`, and
   register each advertised tool as `mcp.<plugin_id>.<tool_name>` with its JSON
   Schema in the Tool Registry, so every call runs the full Tool Bus pipeline
   from §4.2 (schema-validate args → authz/risk gate → audit STARTED → dispatch
   to the MCP client → timeout enforcement → result validation + secret
   redaction → audit finalize + OTel span). Expose MCP resources to the
   Research/Memory agents as `mcp.<id>.read_resource` read tools; surface MCP
   prompts in the prompt library.
3. Handle `notifications/tools/list_changed`: live re-registration of the
   server's tool set in the registry without restarting the plugin or core.
4. Risk defaults per ADR-INT-02: every MCP tool registers as `risk: write`
   unless the operator explicitly downgrades it per tool in configuration —
   external schemas are never trusted to self-declare risk.
5. Auth for the HTTP transport: OAuth 2.1 per the MCP spec, with tokens stored
   only in the secret store — never in manifests, logs, or prompts. Pin the
   supported MCP spec version(s) and add a conformance test against the
   reference server (risk R3 mitigation).

Definition of done: the reference @modelcontextprotocol/server-filesystem
installs via a manifest file only (zero TITAN code changes); its tools appear in
the registry as mcp.<id>.*, execute under full Tool Bus gating with audit rows,
and a PERMISSION_DENIED fires when an agent lacking scopes calls one; a test
that triggers notifications/tools/list_changed proves tools re-register live.
No TODOs in backend code.
```

### Phase 54 — GitHub, Slack & Discord connectors

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 52

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 52 — the plugin runtime (manifest, lifecycle, sandbox,
webhook endpoints, event subscriptions, UI panels).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md
§6.1 (GitHub reference connector) and §6.3 (Slack & Discord notifications),
requirements TITAN-INT-040 and TITAN-INT-042, plus TITAN-INT-011 (connectors
are pure plugins).

Build PHASE 54 — the VCS and chat connectors, as first-party plugins under
plugins/first_party/* with zero modifications to core packages (TITAN-INT-011:
deleting a connector's directory must leave core tests green):
1. GitHub connector: GitHub App auth (fine-grained installation tokens,
   preferred) with PAT fallback for local/free-tier use, App private key in the
   secret store. Contribute the tools github.create_repo, github.open_pr,
   github.update_pr, github.review_pr, github.merge_pr (risk `admin` on
   protected branches), github.create_issue, github.comment, github.get_checks,
   github.search_code.
2. GitHub webhooks: /hooks/github verifying X-Hub-Signature-256; translate
   pull_request, issue_comment, check_suite, and push into TITAN events
   (vcs.pr.review_requested, vcs.checks.completed, …) on Redis Streams so they
   drive the Volume-6 review/deploy workflow.
3. GitHub rate limiting: token bucket fed from X-RateLimit-* response headers;
   below 10% remaining, write tools return UPSTREAM_ERROR/RATE_LIMITED with
   retry_after so the orchestrator reschedules instead of hammering.
4. Slack connector: bot token (chat:write, commands); slack.post_message and
   slack.post_approval — a Block Kit message with Approve/Reject buttons that
   resolves a pending `admin`-gate approval via the interactivity webhook —
   plus the /titan status <project> slash command. Per-project channel routing,
   per-event configurable message templates, subscribed to the event bus
   notification topics; delivery failures retry with exponential backoff (max 5)
   then surface in the dashboard.
5. Discord connector: discord.post_message and discord.post_embed, slash
   commands via the Discord interactions endpoint.

Discord must reuse the Slack connector's channel-routing and template machinery
— do not build a second notification pipeline (Volume 7 sizes Discord small for
exactly this reason).

Definition of done: the GitHub branch → PR → review comments → checks status →
merge lifecycle passes end-to-end (scratch org, or recorded fixtures when
offline); a webhook with a bad signature is rejected 401 AND audited; an `admin`
tool gate approved from a Slack button lets the blocked tool call proceed, with
the approval attributed to the approver in the audit log; a Discord message
posts through the shared routing/template machinery. Tests for every webhook
endpoint (happy path + bad signature). No TODOs in backend code.
```

### Phase 55 — Chaos suite + benchmark harness

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 51, 54

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 51 — derived artifacts, event replay, and a mock-LLM
end-to-end pipeline run; 54 — GitHub/Slack/Discord connectors over the plugin
runtime.

Read fully: Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
§7 (Success Metrics and KPIs table), §9.1 (MVP exit criteria), and requirements
TITAN-FND-007 and TITAN-FND-052; Project-TITAN-Docs/09-Volume-9-Production-and-
Operations.md §3.2 (workflow definitions, nightly.yml).

Build PHASE 55 — crash-recovery proof and the KPI benchmark runner:
1. Chaos suite (TITAN-FND-052 / TITAN-FND-007): scripted tests that kill each
   service in turn (orchestrator, worker, backend API, Redis, PostgreSQL) mid-
   project and assert: zero acknowledged data loss (PostgreSQL is the system of
   record), Redis Streams redelivery covers all in-flight work, and the project
   resumes within 60 seconds of restart with full message history intact.
2. Recovery probes: a WebSocket client stays connected across each kill and
   asserts reconnection plus resumed live event delivery; a post-chaos ledger
   reconciliation job compares the task/gate ledgers against stream
   acknowledgements and proves zero acknowledged-task loss.
3. Benchmark harness: a fixture schema for the fixed 10-brief benchmark suite
   that backs the Section-7 KPI table, and a runner CLI that executes briefs and
   records every Section-7 KPI per run — autonomous completion rate, first-draft
   rejection rate, time-to-first-deploy, local-only viability, cost per project
   (gateway-tracked spend), generated test coverage, crash recovery, and the
   human-intervention count (displayed, never driven to zero — approval gates
   are a feature).
4. Scope control: exactly ONE tiny brief is executed end-to-end in this phase;
   the other 9 briefs ship as fixtures only. Wire the full 10-brief suite into
   the nightly CI workflow (Volume 9 §3.2 nightly.yml), never the per-commit
   pipeline.

Definition of done: the kill-any-service chaos tests pass — no acknowledged data
loss, queue redelivery observed, resume < 60 s — and reconciliation reports zero
acked-task loss; the runner CLI executes the one tiny brief end-to-end and emits
a complete KPI report covering every Section-7 metric. No TODOs in backend code.
```

### Phase 56 — Full-roster offline acceptance run (AGT-007)

**Milestone:** M5 — Autonomous delivery · **Size:** M · **Depends on:** 55

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 55 — the chaos suite and the Section-7 KPI benchmark
runner CLI (this phase uses that runner to record its results).

Read fully: Project-TITAN-Docs/02-Volume-2-Multi-Agent-Intelligence.md
requirement TITAN-AGT-007 (full roster on one laptop, Ollama, offline) and
Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md §9.1
(MVP exit criteria).

Build PHASE 56 — the full-roster offline acceptance run:
1. Run the reference todo-app brief (the §9.1 "build a task-tracking web app"
   exit scenario) with the complete 16-agent roster — CEO, Product Manager,
   Project Manager, Orchestrator/Planner, System Architect, Backend Engineer,
   Frontend Engineer, Database Engineer, DevOps, QA, Security, Code Reviewer,
   Critic, Documentation, Research, Memory — under `docker compose up` with the
   Ollama-only model policy, fully offline after model pulls: zero outbound
   paid-provider calls, verified by egress capture (TITAN-FND-054 style).
2. Fix every defect the run surfaces at its root cause in the owning module
   (scheduler, runtime, gateway, memory, pipeline — wherever it actually lives),
   then re-run until the brief completes. Never stub an agent out, hand-edit the
   generated app, or delete working functionality to get a green run.
3. Capture the run's Section-7 KPIs with the Phase-55 runner CLI and persist the
   KPI report as the acceptance record for TITAN-AGT-007.

Definition of done: the todo-app brief completes end-to-end with all 16 agents
on the Ollama-only profile, offline, under Compose — ending in a blocked/
escalated-to-human state does NOT count as done for this item; the KPI report
for the run is recorded via the Phase-55 runner. No TODOs in backend code.
```

---

## M6 — Dashboard complete

> **Milestone exit state:** All 13 screens live on real data, realtime, responsive, axe-clean

### Phase 57 — Frontend data layer, auth/BFF, notifications & WS client

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 3, 5, 12, 24

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 3 (CI quality gates + the Pydantic→TypeScript OpenAPI bridge),
5 (dashboard shell in apps/web: 13-surface nav, design tokens, top bar with slots for
connection pill and bell), 12 (backend notifications domain module + REST endpoints),
24 (FastAPI /ws realtime backend with {topic, event, seq, ts, payload} envelopes and
topic subscriptions). All earlier phases exist too — in particular the Phase-8 identity
service exposes local JWT auth endpoints.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §4 (State
Management & Data Flow), §5 (Realtime: WebSocket Synchronization), §7 (Notifications
System), §11 (frontend folder structure), ADR-FRT-01 and ADR-FRT-03 in §12, and
requirements TITAN-FRT-014 through 020, 038, 039.

Build PHASE 57 — the frontend data layer every later screen sits on:
1. packages/api-client: TypeScript types generated from the backend OpenAPI schema via
   openapi-typescript plus a typed fetch client; a CI job regenerates types and fails on
   any uncommitted diff (TITAN-FRT-016); an ESLint rule bans `fetch` outside
   packages/api-client and route handlers (TITAN-FRT-014). TanStack React Query v5 owns
   all client server-state with a structured key factory (['project', pid, 'tasks',
   filters]) and RSC initialData via the Hydration API so warm navigation shows no
   refetch flash (TITAN-FRT-017). Zustand stores for pure UI state (shell.ts,
   palette.ts, toasts.ts). Optimistic mutations with rollback on error + retry toast
   (TITAN-FRT-015), with cache invalidation driven by the WS confirmation event
   deduplicated by seq — not the HTTP response. A shared form engine on
   react-hook-form + zod typed from the generated client.
2. Auth: the (auth)/login flow and BFF-only route handlers under app/api (auth callback,
   WS ticket) that hold the session in an httpOnly cookie over the Phase-8 local JWT
   auth endpoints, shaped so swapping in OIDC later is config, not rework (§11 layout).
   lib/auth with JWT/session helpers and useCan RBAC guards for
   owner/admin/developer/viewer — UI hides/disables controls, but the server re-check is
   the authority (TITAN-FRT-037, TITAN-FRT-008).
3. packages/ws-protocol (shared envelope/topic/event TS types for the 8 core topics in
   §5.2) and lib/ws: exactly one multiplexed socket per tab to /ws with JWT
   (TITAN-FRT-018), topic sub/unsub tied to route mount/unmount, 25 s ping/pong,
   monotonic-seq gap detection triggering a targeted React Query refetch (same path as
   reconnect, TITAN-FRT-019), exponential backoff 1 s → 30 s with jitter, and after 60 s
   a manual-retry banner with fallback to 30 s polling on the active screen (§5.3). A
   rAF batching helper for high-frequency topics (TITAN-FRT-021). Wire the top-bar
   connection pill: live / reconnecting (amber) / offline, always visible
   (TITAN-FRT-020).
4. Notifications, consuming the Phase-12 REST endpoints and the Phase-24
   notifications:{userId} topic: sonner toasts (bottom-right, max 3 stacked, 6 s
   auto-dismiss, critical persists until dismissed and is never displaced by
   lower-severity toasts — TITAN-FRT-039); bell + Notification Center popover with
   history, filters, mark-read/mark-all and deep links; the Attention Queue component
   (items requiring human action, never auto-dismissed, mirrored to the bell); the
   5-category taxonomy (approval/failure/progress/activity/security) routed to default
   channels exactly per the §7.1 table. Unread state is server-persisted so the badge
   survives reload and mark-read syncs across tabs ≤ 2 s (TITAN-FRT-038).
5. Storybook configured with both themes for the per-state stories later phases require
   (TITAN-FRT-002), and the bundle-budget scaffold: next build check failing CI when
   /org First Load JS exceeds 300 kB gzipped, with the dynamic-import pattern for heavy
   visualizations established (TITAN-FRT-005).

TypeScript strict with zero `any` in app code (TITAN-FRT-001). No TODOs in backend
code; tests for every new endpoint or route handler.

Definition of done: the CI type-regen job fails on an uncommitted API-type diff; a
forged mutation from a viewer-role session returns 403 and the UI shows a permission
toast; the unread bell badge survives a reload and mark-read propagates to a second
open tab in ≤ 2 s; killing and restoring the WS makes the client detect the seq gap and
refetch affected caches with no stale or duplicate items; the connection pill shows
live → reconnecting → live through that cycle.
```

### Phase 58 — Overview, Projects & intake wizard screens

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 25, 43, 51, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 25 (walking skeleton: a CEO brief already flows to stories
end-to-end), 43 (CEO clarification engine — streamed essential questions, max 5 in one
batch, assumptions logged), 51 (derived artifacts: roadmap, risk analysis, tech-debt
report served as backend endpoints), 57 (frontend data layer: packages/api-client,
React Query + WS sync, useCan guards, notifications surfaces, Storybook).

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.1
(Organization Overview) and §6.2 (Projects List & Project Detail) including
requirements TITAN-FRT-022 and TITAN-FRT-023, plus the four-state rule TITAN-FRT-002
in §1.4 and the screen-state conventions at the top of §6.

Build PHASE 58 — the first live screens on real backend data:
1. Organization Overview at /org per §6.1: four KPI stat cards (projects, agents
   working, tasks with blocked count, cost today with budget bar) each deep-linking to
   a filtered screen (e.g. "5 blocked" → board filtered state=blocked); active-project
   cards showing progress, sprint, agent count, CI status; a virtualized live-activity
   feed that prepends with a 200 ms highlight; and the Needs-Your-Attention queue whose
   items open an approval dialog with context and Approve/Reject/Comment, actionable in
   ≤ 2 clicks (TITAN-FRT-022). Subscribe org:{oid}:activity, agents:status,
   monitoring:summary; KPIs patch in place and panels degrade independently on error;
   empty state is the onboarding hero with a prompt input.
2. Projects list at /projects and detail at /projects/[pid] per §6.2: filterable,
   sortable list rows (status, progress, agents, CI, last activity); detail tabs
   Overview | Board | Graph | Architecture | Deployments | Files | Settings (tabs owned
   by later phases may be labelled stubs — allowed ONLY for pages, never backend code);
   milestone timeline; roadmap, risk register, and tech-debt report rendered from the
   Phase-51 backend Markdown artifacts; Pause/Resume gating all agent work with a
   confirmation dialog, RBAC admin|owner. The New Project intake wizard is a chat-style
   flow: the CEO Agent's clarification questions stream token-by-token, and the result
   is a rendered roadmap + milestone plan on the detail Overview tab with no page
   reload (TITAN-FRT-023) — show streaming progress narration and a skeleton roadmap
   placeholder so slow local models don't feel broken (risk R-3).
3. Storybook stories for all four states (loading/empty/error/populated) for both
   screens (TITAN-FRT-002), with skeleton loaders shaped like their content, not
   spinners.

Extend what exists — the intake Q&A must stream from the Phase-43 CEO clarification
engine and the roadmap view must read the Phase-51 derived roadmap artifact; do not
build a second clarification or roadmap mechanism.

Definition of done: E2E — the backend creates an approval item, it appears on the
Overview within 2 s, and approving it via the dialog takes ≤ 2 clicks and lets the
workflow proceed; the intake wizard visibly streams CEO questions live and ends on a
rendered roadmap without reload; four-state stories exist for every screen built here.
```

### Phase 59 — Kanban board

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 41, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 41 (pipeline & task state machines — the per-project workflow
definition that names the board's columns and legal transitions), 57 (frontend data
layer: typed api-client, React Query with optimistic mutations + seq-deduplicated WS
reconciliation, useCan guards, Storybook).

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.3 (Kanban
Board) with requirements TITAN-FRT-024 and TITAN-FRT-025, §4.2 (mutation rules), and
the Board-scoped keyboard bindings in §10.

Build PHASE 59 — the Kanban board at /projects/[pid]/board:
1. The board per §6.3: columns mapped to the workflow states from the Phase-41
   per-project workflow definition (configurable, with WIP-limit indicators); task
   cards showing ID, title, agent avatar, points, labels, and blocked badge; a task
   detail Sheet (right slide-over, deep-linked via ?task=T-231) with description,
   acceptance criteria, dependency links, linked PR/conversation, and full event
   history. Drag-and-drop with @dnd-kit: optimistic move, RBAC-gated (a human move is
   an override that notifies the PM Agent), rollback + retry toast on failure
   (TITAN-FRT-015), and server-wins reconciliation against task.moved/updated/created
   WS events deduplicated by seq — a card moved by an agent animates 300 ms to its new
   column with the acting agent's avatar pulsing; a conflicting local drag resolves
   server-wins with a toast. Column stream failure shows a stale-data badge on that
   column only.
2. Keyboard DnD per §10 with the dnd-kit keyboard sensor: arrows navigate cards, Enter
   opens the Sheet, Space lifts the card, arrows move it, Space drops, Esc cancels —
   with pick-up/over/drop announced via aria-live (TITAN-FRT-025, TITAN-FRT-043).
3. A concurrent-move convergence test harness: a chaos E2E fires 20 concurrent agent +
   human moves (agent moves injected via API while the human drags) and asserts the
   board converges to exactly the API state with no ghost or duplicated cards
   (TITAN-FRT-024); plus the kill-API-mid-drag test proving the card returns to its
   origin column with a retry toast.

Four-state Storybook stories for the board (loading = 5 skeleton columns × 3 cards;
empty = "No tasks yet — the Project Manager Agent creates tasks after planning
completes").

Definition of done: the 20-concurrent-move chaos test converges to API state with no
ghost cards; a keyboard-only move completes end-to-end with screen-reader
announcements; the mid-drag API failure rolls back the card and shows a retry toast.
```

### Phase 60 — Graph engine, task graph & Architecture Viewer

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 57 (frontend data layer: typed api-client, React Query + WS
sync with seq reconciliation, Storybook, and the dynamic-import bundle-budget
scaffold).

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.4 (Task
Graph / Dependency Graph, TITAN-FRT-026), §6.8 (Architecture Viewer, TITAN-FRT-031),
ADR-FRT-04 in §12, and TITAN-FRT-047 (accessible list-view alternative) in §9.

Build PHASE 60 — one shared graph engine and its two consumers:
1. The shared engine in components/graph on React Flow (@xyflow/react) + dagre
   auto-layout (ADR-FRT-04): a custom node-type registry, mini-map, fit/zoom controls,
   clustered group rendering with drill-down as the performance guard beyond 500 nodes,
   incremental re-layout limited to the affected subgraph (full re-layout only on an
   explicit "Re-layout" click), and a toggleable "List view" exposing the same
   nodes/edges/actions as an accessible alternative (TITAN-FRT-047). Dynamically
   imported so the /org bundle budget holds (TITAN-FRT-005).
2. Task/Dependency graph at /projects/[pid]/graph per §6.4: custom task nodes (status
   icon, ID, title, agent avatar), dashed-red blocked edges, a client-computed
   critical-path toggle (longest incomplete chain), a selection inspector bar with the
   selected node encoded as ?node= in the URL, hover highlighting of upstream/
   downstream chains, and Shift+click multi-select for bulk label ops. Layout is
   read-only — dependencies are edited by agents; humans request changes via the task
   Sheet. Realtime: status patches recolor nodes in place; new tasks fade in with
   incremental re-layout.
3. Architecture Viewer at /projects/[pid]/architecture per §6.8, reusing the same
   engine with C4 node types (system/container/component/datastore/external): a
   Context → Container → Component level switcher with double-click drill-down; a
   version timeline where every revision the Architect Agent commits is selectable; a
   visual diff mode between any two versions (added = green outline, removed = red
   ghost, changed = amber) satisfying TITAN-FRT-031; an ADR side panel rendering
   Context/Decision/Alternatives/Trade-offs from backend records with a full-record
   dialog; client-side SVG/PNG export (offline-safe); and an architecture.updated
   banner "New version vN available — [View]" instead of mutating the canvas under the
   user.

Four-state Storybook stories for both screens; Graph keyboard bindings per §10
(+/- zoom, 0 fit, arrows pan, Tab cycles nodes).

Definition of done: the CI perf test pans/zooms a seeded 300-node/600-edge fixture at
≥ 30 fps; reloading a ?node= deep link restores the identical selection and inspector;
an E2E that creates 3 architecture revisions asserts the diff highlights match the
fixture.
```

### Phase 61 — Agent grid + Live Conversations

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 57 (frontend data layer: typed api-client, React Query + WS
sync, notifications bell + toasts, rAF batching helper). The backend already serves
agent status heartbeats on agents:status and per-channel messages with
message.created / message.chunk token streams on channel:{cid}:messages.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.5 (Agent
Status Grid, TITAN-FRT-027) and §6.6 (Live Agent Conversations, TITAN-FRT-028 and
TITAN-FRT-029), plus the Conversations keyboard bindings in §10.

Build PHASE 61 — realtime agent visibility:
1. Agent Status Grid at /agents per §6.5: a responsive card grid grouped by hierarchy
   tier (executive / management / engineering / quality); each card shows role, state
   chip (working / idle / blocked / error / paused), current task link, model+provider
   chip in use, token throughput, and elapsed time; header roll-up counts (working /
   idle / error); the agent detail drawer at /agents/[agentId] as an intercepted
   parallel route (@drawer per the §11 layout) with KPIs, recent messages, memory
   usage, and Pause/Restart/Reassign actions (RBAC admin+, with confirmation).
   Heartbeats arrive on agents:status at 5 s cadence; 3 missed heartbeats degrade the
   card to "stale" with a last-seen timestamp — the grid never blanks; agent error
   events raise a toast + bell notification, and clicking the model chip opens
   provider settings (RBAC-gated).
2. Live Conversations at /conversations and /conversations/[channelId] per §6.6:
   channel list (per-project + org channels) with unread badges; a virtualized message
   feed with cursor-based scroll-up history pagination; every message rendered with
   its typed chip for all 8 Volume 2 message types (TASK_ASSIGNMENT, PROGRESS_REPORT,
   REVIEW_REQUEST, BUG_REPORT, DECISION, BLOCKER, MEETING_SUMMARY,
   CONFLICT_RESOLUTION), each deep-linking to its referenced entity — task, PR, ADR,
   deployment (TITAN-FRT-029); token-streaming bubbles that grow from message.chunk
   events with a typing caret, applied via rAF batching; a human input box posting as
   role `client` with Ctrl/⌘+Enter send; auto-scroll only when the user is at bottom
   (a "↓ N new" pill otherwise); message-type filter, mute channel, copy permalink,
   history search, Alt+↑/↓ channel switching. Disconnection keeps history and shows a
   "stream interrupted — reconnecting" divider.

Four-state Storybook stories for both screens; the streaming caret must respect
prefers-reduced-motion (TITAN-FRT-045).

Definition of done: a fault-injection E2E drives an agent into error and its card plus
a bell notification appear within 2 s, with a one-click path to that agent's logs; a
perf trace of a 2,000-token streamed message keeps CLS ≈ 0 with first token painted
< 500 ms after receipt; deep links resolve for all eight message types.
```

### Phase 62 — Logs Viewer + Loki history endpoint

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 4, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 4 (observability foundation in Compose — Loki + promtail
already ingest every service's structured JSON logs), 57 (frontend data layer: typed
api-client, WS client with reconnect/seq-gap detection, rAF batching helper). The
backend already streams log.appended events on logs:{stream} topics.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md — §6.7 (Logs
Viewer, TITAN-FRT-030), ADR-FRT-05 in §12, and TITAN-FRT-021 (rAF batching); plus
Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §6.3 (Loki log
aggregation: labels {namespace, service, level} only, everything else queried from the
JSON body) for the history endpoint's data source.

Build PHASE 62 — high-throughput logs with backend history fill:
1. Backend: a log-history REST endpoint that queries Loki (e.g.
   GET /api/v1/logs/history) with filters matching the viewer's filter bar — stream,
   project, agent, level, time range, text/regex — plus cursor pagination, returning
   structured log lines for gap fill and on-demand older history. Query Loki by its
   low-cardinality labels and filter the rest from the JSON body per Volume 9 §6.3.
   No TODOs in backend code; integration tests for the endpoint (happy path, filter
   combinations, auth failure).
2. Logs Viewer at /logs per §6.7: a TanStack Virtual monospace list over an in-memory
   ring buffer capped at 50k lines (ADR-FRT-05); log.appended events applied in
   batches ≤ 100 lines per frame via requestAnimationFrame (TITAN-FRT-021); a filter
   bar (stream, project, agent, level, time range, regex with match highlighting) with
   every filter URL-encoded and restorable; follow-tail toggle that auto-pauses on
   scroll-up (Space pauses, `e` jumps to next ERROR per §10); line expansion for
   structured JSON payloads; click on an ERROR line opens a context drawer with ± 50
   lines and links to the owning agent/task; copy-line and filtered NDJSON export;
   level color-coding always paired with icons (TITAN-FRT-044). When a filter is
   active with tail paused, expose full-history search via the new backend endpoint —
   client-side filtering only searches the buffer.
3. Reconnect gap handling: after a WS drop, insert a "reconnected · N lines skipped
   [load]" divider marker; clicking [load] lazily fetches exactly the skipped window
   from the Loki history endpoint and splices it into the buffer. Stream errors show a
   banner with retry while retaining the existing buffer.

Four-state Storybook stories for the screen (empty = "No log lines match filters" with
one-click filter reset).

Definition of done: the CI perf fixture sustains 1,000 lines/s in tail mode at ≥ 30 fps
with filters still responsive (TITAN-FRT-030); dropping and restoring the WS during a
log burst produces a gap marker whose [load] fills the skipped lines from the Loki
endpoint with no duplicates; the history endpoint's integration tests pass.
```

### Phase 63 — Deployment, Monitoring & Memory Explorer screens

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 17, 40, 48, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 17 (LLM gateway cost accounting — every ChatResponse.usage
row in the llm_usage table with cost_usd from config/prices.yaml, plus budget caps),
40 (memory decay/curation with pin/archive tiers, lessons, and the append-only
brain.memory_ledger trail), 48 (generated-project git workspaces and the local
Docker Compose deploy via the deploy.compose tool), 57 (frontend data layer:
packages/api-client, React Query + RSC hydration, auth/BFF, notifications, WS client).

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §6.9–6.11
(Deployment Dashboard, Monitoring, Memory Explorer) including TITAN-FRT-032, -033,
and -034. Skim Volume 5 §12 (memory API endpoints, ledger trail) and Volume 7's
cost-accounting section (llm_usage) for the backing contracts.

Build PHASE 63 — the ops-facing screens: Deployments, Monitoring, Memory Explorer:
1. Extend the Phase-48 local deploy with a minimal deployments API: list
   deployments/environments, redeploy-previous (rollback to the prior version's
   record) and abort of an in-flight run, all executed over Docker Compose
   (deploy.compose). Environment cards are scoped to the local Compose environments
   that actually exist — do not fabricate staging/prod backends, but implement the
   confirmation/RBAC gate logic generically so later environments slot in.
2. Deployment Dashboard (/deployments and /projects/[pid]/deployments) per §6.9:
   environment cards (version, health, URL, uptime), pipeline run visualization with
   stage chips (build → test → scan → deploy) showing per-stage duration and
   expandable live step logs reusing the §6.7 log list, deploy history with rollback
   causes. Promote/Rollback/Abort are approval-gated: confirmation dialog restating
   scope, typed environment name for prod promote AND for rollback, RBAC owner|admin
   only, 🔒 lock shown while a release awaits CEO-Agent/human approval (TITAN-FRT-032:
   viewer sees no action buttons at all). Realtime: deployment.status_changed and
   deployment.step animate stage chips; a failed prod deploy raises a critical
   notification.
3. Monitoring (/monitoring) per §6.10: stat tiles with sparklines and threshold
   coloring (API p95, error rate, queue depth, LLM spend/hr); dynamically-imported
   Recharts time-series for latency p50/p95/p99, error rate, queue depth, and token
   usage/cost grouped by provider and by agent, sourced from the Phase-17 llm_usage
   ledger (TITAN-FRT-033); range picker 15m/1h/24h/7d; platform / per-generated-app
   scope switcher; alert list with Ack (RBAC admin+); "Open in Grafana" deep links to
   the local Compose Grafana — embed nothing from Grafana. metric.sample (1 Hz) rolls
   tiles live; historical ranges are REST-fetched; datasource loss flags "stale @ HH:MM".
4. Memory Explorer (/memory) per §6.11: tier tree (short-term / project / org /
   patterns / lessons), Qdrant-backed semantic search with score-ranked top-20
   results and the "No semantic matches ≥ 0.6" miss state, faceted browse (type,
   project, date, agent), detail panel showing content preview, embedding meta, full
   provenance chain (creating agent, source entity, timestamp — TITAN-FRT-034) and
   usage history rendered from the memory_ledger trail (GET /memory/records/{id}),
   plus curation actions: Pin, Edit tags, Archive (soft-delete, RBAC admin+).
   memory.updated events badge the affected tier ("3 new") — refresh on demand, never
   mutate the list under a reading user.

Extend the Phase-48 deploy.compose mechanism for all deployment actions — do not
build a parallel deploy system; staging promotion and blue/green arrive in later
phases. All four states (loading/empty/error/populated) per screen. Tests for every
new endpoint. No TODOs in backend code.

Definition of done: rollback prompts for the typed environment name and, on confirm,
redeploys the previous version via the deployments API (verified by E2E); the
Monitoring cost-by-provider chart totals match a seeded llm_usage fixture exactly;
a fixture memory entry renders its complete provenance chain from the ledger trail
with all links resolving.
```

### Phase 64 — Plugin Manager + Settings screens

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 17, 53, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 17 (gateway provider config, cost accounting and budget
caps — provider switching is already config-only server-side), 53 (MCP client:
add-by-URL/command servers with tool discovery), 57 (frontend data layer,
auth/BFF + RBAC guards (useCan), notifications, WS client). The plugin runtime from
Phase 52 already loads manifests declaring tools, webhooks, and ui_panels.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §6.12–6.13
(Plugin Manager, Settings) including TITAN-FRT-035, -036, -037, and §7 (notification
categories for the Settings matrix). Skim Volume 7 §on plugin manifests (ui_panels,
permission scopes, TITAN-INT-011).

Build PHASE 64 — Plugin Manager and the 7 Settings sub-pages:
1. Plugin Manager (/plugins) per §6.12: three tabs — Installed (status, version,
   health-check result, config gear, per-plugin activity log), Catalog (install cards
   listing exactly which capabilities/scopes each plugin requests, per the Volume 7
   permission model; catalog fetch failure falls back to a bundled offline catalog),
   and MCP Servers (add by URL/command via the Phase-53 client, tool-list preview,
   enable per-agent). Install flow: permission consent dialog → config form →
   test-connection → enabled; Disable/Uninstall confirm and state data-retention
   behavior. Config forms are generated from each plugin's JSON-Schema manifest by
   one shared form engine — no hand-built form per plugin (TITAN-FRT-035). Secret
   fields are write-only: masked as •••• after save and never returned by any API.
   plugin.status_changed updates health chips live.
2. Named dashboard extension slots that render the ui_panels declared in plugin
   manifests (slot name + sandboxed panel src per Volume 7), so a plugin can
   contribute UI with zero modifications to core packages (TITAN-INT-011).
3. Settings (/settings/*) per §6.13, seven independently-loading RSC sub-pages:
   Profile (name, email, avatar, session list with revoke); Appearance (theme
   dark/light/system, density, reduced-motion override, language); LLM Providers
   (provider keys, default + fallback chain, per-agent-role model overrides, budgets
   with hard-stop; "Test" performs a live round-trip reporting latency and model
   list; switching the default provider is Settings-only and takes effect without
   redeploy — TITAN-FRT-036); Members & Roles (invite, owner/admin/developer/viewer,
   per-project grants); API Keys (scoped personal access tokens shown exactly once);
   Notifications (per-category channel matrix over the §7 taxonomy: approval,
   failure, progress, activity, security × toast/bell/attention-queue/plugins);
   Budgets & Limits (token/cost caps per project, wired to the Phase-17 enforcement).
   All forms react-hook-form + zod with inline field errors, Save disabled until
   dirty, typed-confirmation for destructive actions (remove member, revoke key).
4. RBAC everywhere: controls hidden/disabled by role in the UI AND re-checked
   server-side on every mutation — a forged viewer request returns 403 and the UI
   shows a permission toast (TITAN-FRT-037).

All four states (loading/empty/error/populated) per screen. Tests for every new
endpoint. No TODOs in backend code.

Definition of done: a sample fixture plugin's panel renders in a named slot with no
core-code changes; E2E switches the default provider Anthropic → Ollama via Settings
and the next agent action logs the new provider; a saved plugin secret displays as
•••• and a network-capture assertion proves it appears in no API response body.
```

### Phase 65 — Command palette, shortcuts, responsive & a11y sweep

**Milestone:** M6 — Dashboard complete · **Size:** M · **Depends on:** 58, 59, 60, 61, 62, 63, 64

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 58–64 — all 13 dashboard surfaces exist (Overview,
Projects + intake, Kanban, Task Graph, Architecture Viewer, Agent Grid,
Conversations, Logs, Deployments, Monitoring, Memory Explorer, Plugin Manager,
Settings), on the shared data layer, WS client, and design-system tokens.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §8 (Responsive
Behavior), §9 (Accessibility), §10 (Keyboard Shortcuts), plus the §1.4 global
requirements TITAN-FRT-002/004/005 and requirement rows TITAN-FRT-041 through -044
(and the adjacent -045/-046 rows they lean on).

Build PHASE 65 — cross-screen polish over every existing surface:
1. Command palette (Ctrl/⌘+K) in components/shell: fuzzy search navigating to any
   screen, project, agent, or task; opens ≤ 100 ms; typing "kan net" surfaces the
   netflix-clone board (TITAN-FRT-007). Add the Ctrl/⌘+P project switcher.
2. Shortcut engine (lib/shortcuts): global registry with Gmail-style sequences —
   `g` then o/p/b/g/a/c/l/d/m for Org/Projects/Board/Graph/Agents/Conversations/
   Logs/Deployments/Monitoring — with 1.5 s sequence timeout; Esc closes top-most
   layer; Ctrl/⌘+Enter submits; `n` contextual new; `/` focuses the screen's primary
   search; shortcuts disabled inside text inputs except Esc/⌘Enter; never override
   browser defaults. Screen-scoped bindings: Board (arrows navigate, Enter opens,
   Space lift/move/drop via the dnd-kit keyboard sensor), Graph (+/- zoom, 0 fit,
   arrows pan, Tab cycles nodes), Logs (Space pause tail, `e` next ERROR),
   Conversations (Alt+↑/↓ channel switch). A `?` overlay lists every binding
   (TITAN-FRT-048).
3. Responsive sweep per §8 across all 13 screens: icon-rail sidebar at 1024–1279;
   at 768–1023 Kanban horizontal-scrolls with snap columns, Overview KPI grid goes
   2×2, detail panels become bottom Sheets; under 768 the sidebar becomes a hamburger
   Sheet with a bottom tab bar (Org/Board/Agents/Convos/More), one panel at a time,
   Graph/Architecture render read-only with pinch-zoom, tables become card lists.
   Touch targets ≥ 44 px under md; drag-and-drop gains long-press initiation plus an
   explicit "Move to…" menu fallback; wide content scrolls in its own container —
   never the body (TITAN-FRT-041).
4. Accessibility sweep per §9: full keyboard operability with visible 2 px-offset
   --ring focus ring, logical order, no traps, skip-to-content link (TITAN-FRT-042);
   landmarks and aria-live regions — activity feed and toasts polite, critical alerts
   assertive, Kanban DnD announcements via dnd-kit (TITAN-FRT-043); no color-only
   status — every colored state pairs an icon/text glyph ✔ ⚠ ✖ ◐ (TITAN-FRT-044);
   prefers-reduced-motion disables non-essential animation while data still updates
   (TITAN-FRT-045); 200% zoom loses no function (TITAN-FRT-046).

This phase modifies existing screens — do not fork alternate mobile pages; adapt the
existing components in place behind the §8 breakpoints.

Definition of done: the 375 px Playwright suite is green on all 13 screens with zero
horizontal body scroll and all primary actions reachable; axe-core reports zero
serious/critical violations in both dark and light themes; the palette and shortcut
engine can reach and operate every screen keyboard-only, and the ? overlay matches
the §10 table.
```

### Phase 66 — Frontend test & CI enforcement

**Milestone:** M6 — Dashboard complete · **Size:** S · **Depends on:** 65

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 65 — all 13 dashboard screens are built, responsive to
375 px, accessible, and operable via palette + shortcuts; Storybook/Playwright
infrastructure and the CI pipeline exist from earlier phases.

Read fully: Project-TITAN-Docs/04-Volume-4-Frontend-and-Dashboard.md §1.4 global
requirements (TITAN-FRT-001 through -005) and the §9–10 acceptance rows
(TITAN-FRT-045 through -048) whose E2E checks this phase must automate in CI.

Build PHASE 66 — make the frontend quality bar machine-enforced:
1. Four-state story enforcement: every one of the 13 screens has Storybook stories
   for loading, empty, error, and populated states, snapshotted by the visual
   regression suite (TITAN-FRT-002). Add a CI coverage check that maps each screen to
   its 4 required stories and fails when any is missing — a new screen cannot merge
   without its states.
2. Playwright offline-mode suite: the full E2E run passes against the local Docker
   Compose backend with the machine offline except loopback (local LLMs via Ollama),
   proving TITAN-FRT-004; fold in the automated acceptance runs for reduced-motion
   (TITAN-FRT-045), 200% zoom (TITAN-FRT-046), and the shortcut-overlay snapshot
   (TITAN-FRT-048) so they gate CI, not just exist.
3. Bundle budget: First Load JS for the /org route ≤ 300 kB gzipped, enforced by a
   `next build` budget check that fails CI on breach; verify heavy visualizations
   (React Flow graphs, Recharts, log viewer) stay dynamically imported
   (TITAN-FRT-005).
4. Wire all three into the existing CI pipeline as blocking jobs alongside the
   already-enforced tsc strict/no-any, ESLint import boundaries, and OpenAPI
   type-regeneration checks — no advisory-only jobs.

Do not add a second CI pipeline — extend the existing workflow with these jobs.

Definition of done: deliberately deleting one screen story fails CI on the coverage
check, and inflating /org past 300 kB gzipped fails the budget check (demonstrate
both, then revert); the offline Playwright suite runs green in CI.
```

---

## M7 — Hardened & production-ready

> **Milestone exit state:** Red-team corpus blocked, lockdown drill <=60s, PITR restore verified, readiness checklist signed

### Phase 67 — Auth hardening: OIDC service, Argon2id, throttling, MFA

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 8, 9, 57

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 8 (local identity: AUTH_MODE=local bootstrap
admin, RS256 access JWTs ≤15 min with sub/org/roles/scope/jti, rotating refresh
tokens with family reuse detection, Redis jti denylist, API keys, service
tokens); Phase 9 (RBAC engine with Requires("resource:action") on every route);
Phase 57 (frontend data layer with the Next.js BFF holding session tokens in
httpOnly cookies).

Read fully:
- Project-TITAN-Docs/08-Volume-8-Security.md §3.1–3.2 (auth architecture and
  TITAN-SEC-010 through SEC-017, especially SEC-010/015/017) and ADR-SEC-01
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md §9.2
  (TITAN-BCK-020, the pluggable-IdP + local password mode it defines)

Build PHASE 67 — auth hardening:
1. The embedded OIDC-compatible auth service from Volume 8 §3.1: FastAPI +
   authlib, username/password login, OAuth2 Authorization Code + PKCE only —
   implicit and password grants disabled at the endpoint level so an
   implicit-flow attempt returns an OAuth error (TITAN-SEC-010). Production
   federation to any external OIDC IdP (Keycloak/Google/GitHub/Okta) stays a
   config-only switch behind the same interface.
2. Argon2id for all embedded-IdP password hashes (migrate existing Phase-8
   hashes on next login), plus login-failure throttling: per-account AND
   per-IP exponential backoff after 5 failures, with audit events
   (TITAN-SEC-017).
3. TOTP MFA in the embedded IdP: enrollment (QR + recovery codes), verification
   at login, and an org security policy flag that REQUIRES MFA for Admin/Owner
   roles — enforced at the next privileged login after the policy toggles
   (TITAN-SEC-015).
4. Verify the Phase-57 BFF flow end-to-end against this real OIDC path: PKCE
   redirect → code exchange at the BFF → httpOnly, Secure, SameSite=Lax
   session cookies, tokens never in localStorage or client JS (TITAN-SEC-013,
   ADR-SEC-01).

This EXTENDS the Phase-8 identity stack — do not build a second token issuer.
TITAN still mints its own RS256 JWTs after the OIDC exchange, and
AUTH_MODE=local (direct password login for laptop-only use) is retained.
Record the reconciliation of TITAN-SEC-010 ("password grant disabled") with
TITAN-BCK-020 ("local password mode") as an ADR in the repo: the local mode is
a first-party login form on the embedded IdP, not an OAuth password grant,
which stays disabled.

Definition of done: an integration test round-trips the full Authorization
Code + PKCE flow through the BFF; requests using implicit or password grant
are rejected with an OAuth error; a scripted brute-force run trips backoff on
both the account and the source IP and the DB shows Argon2id hashes; with an
org's MFA-required policy on, an Admin login without a valid TOTP code fails
and succeeds with one. Tests for every new endpoint; no TODOs in backend code.
```

---

### Phase 68 — Secrets vault, scoped injection, encryption & signed envelopes

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 17, 18, 20

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 17 (LLM gateway resilience + cost accounting —
the gateway is already the single caller of provider APIs); Phase 18 (Tool
Registry + Tool Bus pipeline with per-agent grants); Phase 20 (per-task
workspace sandboxes: non-root, deny-all egress, destroyed after task).

Read fully:
- Project-TITAN-Docs/08-Volume-8-Security.md §6.1–6.3 (secret classes, vault
  architecture, delivery: TITAN-SEC-050/051/052/053), ADR-SEC-03, §3.5
  (TITAN-SEC-020 signed queue envelopes), and §7 TITAN-SEC-063 (field-level
  encryption)
- Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md
  (structured message envelope, TITAN-FND-006, and secure-defaults
  TITAN-FND-062)

Build PHASE 68 — the secrets layer:
1. A thin SecretsProvider interface (get-by-reference, rotate, list-metadata
   per ADR-SEC-03) with the SOPS+age driver as default: age-encrypted YAML
   files unlocked at boot into an in-memory secrets service — plaintext lives
   nowhere else, never in code, git, plain config, Redis, or Postgres app
   tables (TITAN-SEC-050). Write-only API for humans: create/rotate/delete
   only, no read-back via any API or UI — read attempts return 405/404
   (TITAN-SEC-051). Every secret carries metadata (owner, scope
   org/project/task, timestamps, rotation period default 90 days) and overdue
   secrets raise dashboard expiry warnings (TITAN-SEC-052).
2. Scoped injection (TITAN-SEC-053): platform services get env at boot;
   Phase-20 sandboxes get ONLY the secrets their task's tool grants require,
   as tmpfs files destroyed with the sandbox; LLM provider keys exist solely
   in the gateway process — agents never see them.
3. Signed HMAC envelopes on every Redis Streams message (TITAN-SEC-020):
   {msg_id, sender_principal, task_id, ts, payload_sha256, hmac} with
   per-service keys pulled from the vault; consumers drop invalid signatures
   and write an audit event.
4. AES-256-GCM field-level encryption (key from the vault, key-version
   tagging for rotation) for the highest-sensitivity columns: stored OAuth
   tokens and webhook signing secrets (TITAN-SEC-063).

Definition of done: secret read attempts return 405/404; an env + filesystem
dump inside a sandbox running a docs task contains zero integration secrets
while the gateway alone holds provider-key env vars; a deliberately tampered
queue message is dropped and produces an audit event; a raw pg_dump shows
only ciphertext for the encrypted columns and gitleaks over the repo finds
nothing. Tests for every new endpoint; no TODOs in backend code.
```

---

### Phase 69 — Redaction engine (three sinks) + secret-file exclusion

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 4, 35, 68

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 4 (observability foundation: Loki + promtail
in Compose, structured JSON logs); Phase 35 (Qdrant collections + embedding
ingestion pipeline); Phase 68 (secrets vault — the live secret inventory the
redactor matches against).

Read fully:
- Project-TITAN-Docs/08-Volume-8-Security.md §6.4 (TITAN-SEC-054/055/056),
  §5 TITAN-SEC-046 (sandbox output redaction), and §7 (encryption context
  around TITAN-SEC-063)

Build PHASE 69 — the redaction engine:
1. One shared redaction pipeline (TITAN-SEC-054) combining three detectors:
   exact-match against the live Phase-68 secret inventory via hashed lookup,
   high-entropy string detection, and provider-key patterns (sk-ant-*, ghp_*,
   AKIA*, …). Matches are replaced with [REDACTED:<class>:<hash8>]. Wire it
   into all three sinks: (a) every context bundle in the LLM gateway BEFORE
   it is sent to any provider, (b) every log line before Loki ingestion,
   (c) every sandbox stdout/stderr stream before it reaches logs, the UI, or
   LLM context (TITAN-SEC-046). Redaction hits emit audit events.
2. Secret-bearing file exclusion (TITAN-SEC-055): the patterns .env*, *.pem,
   *_key*, id_rsa*, and cloud credential paths are excluded by default from
   context assembly AND from the Phase-35 Qdrant/RAG ingestion pipeline — a
   repo containing a .env never surfaces it in prompts or vectors.

Extend the Phase-68 secrets service for the inventory lookup and the Phase-4
logging pipeline for the Loki sink — do not build parallel log shippers or a
second secret store.

Definition of done: a test plants one secret of each class (inventory secret,
high-entropy token, provider-pattern key) and asserts it appears redacted at
all three sinks, including a captured gateway request payload; gitleaks over
repo and logs finds nothing; an ingestion test over a repo with .env and
id_rsa shows those files absent from both assembled context and Qdrant.
No TODOs in backend code.
```

---

### Phase 70 — Input validation, rate limiting, SSRF & webhook hardening

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 12, 23, 54

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 12 (domain modules II: agents, messages,
decisions, runs, artifacts, notifications endpoints + queue payloads);
Phase 23 (web tools with registry mirror and egress proxy — provides
web.fetch); Phase 54 (GitHub/Slack/Discord connectors with inbound webhook
endpoints).

Read fully:
- Project-TITAN-Docs/08-Volume-8-Security.md §8 (TITAN-SEC-070 through
  SEC-076) and §9 (TITAN-SEC-080/081)
- Project-TITAN-Docs/03-Volume-3-Backend-Engineering-and-Infrastructure.md
  §10 (TITAN-BCK-022 + route-class table) and §11 (TITAN-BCK-023 caching)

Build PHASE 70 — boundary hardening:
1. Strict validation everywhere (TITAN-SEC-070): every API request body and
   every queue message validated by Pydantic v2 models with extra="forbid",
   explicit types, length bounds, and enum/pattern constraints; failures
   return 422 WITHOUT echoing raw input. Error responses become generic —
   correlation ID only, stack traces never reach clients (TITAN-SEC-076).
   Add a semgrep CI rule banning string-built SQL anywhere including
   migrations (TITAN-SEC-071); all DB access stays SQLAlchemy-parameterized.
2. Redis token-bucket rate limiting via an atomic Lua script (EVALSHA), keyed
   per principal and route class (TITAN-BCK-022): read 300/min burst 60,
   write 60/min burst 20, :transition + run creation 30/min, auth 10/min per
   IP, project creation 10/hour — layered with the Volume 8 per-IP pre-auth
   (60/min), per-user (600/min), and per-org buckets (TITAN-SEC-080).
   Responses carry 429 + Retry-After + X-RateLimit-{Limit,Remaining,Reset}
   on both 2xx and 429. WebSocket caps with backpressure-then-disconnect and
   an audit event on sustained violation (TITAN-SEC-081). Internal agent
   traffic bypasses per-route limits (the platform must never rate-limit
   itself into deadlock). Add the read-through Redis caching tier for hot
   endpoints (GET /projects/{id}, /board, /agents) with version-bump
   invalidation and titan_cache_{hits,misses}_total metrics (TITAN-BCK-023).
3. SSRF guard on web.fetch and all outbound platform fetches (TITAN-SEC-074):
   resolve DNS, reject private/link-local/metadata IP ranges, re-validate on
   every redirect hop, http(s) only, blocked attempts audited. Inbound
   webhooks (Phase-54 connectors) verify HMAC signatures and reject unsigned
   or stale deliveries (>5 min skew) with 401; replays rejected
   (TITAN-SEC-075).

Definition of done: a hypothesis-based fuzz run over the public endpoints
yields zero 500s; the semgrep rule fires on a committed string-SQL fixture;
a k6 burst test shows hard cutoff at the configured limits with rate-limit
headers present; an SSRF corpus (169.254.169.254, 127.0.0.1, redirect-to-
internal, file://) is fully blocked and audited through web.fetch; an
unsigned webhook returns 401. Tests for every new middleware branch; no
TODOs in backend code.
```

---

### Phase 71 — Budget tier extension, loop detection & frontend CSP

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 17, 57, 70

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 17 (gateway cost accounting: llm_usage table,
versioned price table, Redis rolling counters, per-task/per-project budget
caps with the resumable budget_hold pause state); Phase 57 (frontend data
layer, auth/BFF, notifications); Phase 70 (validation + rate limiting
framework).

Read fully:
- Project-TITAN-Docs/08-Volume-8-Security.md §8 (TITAN-SEC-072/073) and §9
  (TITAN-SEC-082/083)
- Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §10 (budget
  hierarchy: global monthly → org → project → task, 80%/100% behavior,
  budget_hold)

Build PHASE 71 — spend and abuse containment:
1. Extend the budget system to the full TITAN-SEC-082 tier set: per-task
   (exists), per-project/day, and per-org/month caps, with local models
   time-capped instead of dollar-capped. At 80% of a tier: in-app + ticket
   notification; at 100%: hard stop — new LLM calls rejected, the task
   pauses into the existing resumable budget_hold state, and a Maintainer is
   notified rather than output being silently truncated. This ADDS tiers ON
   TOP of the Phase-17 Redis rolling counters and budget_hold machinery —
   do not build a parallel budget mechanism.
2. Loop detection (TITAN-SEC-083): >=3 near-identical consecutive tool calls
   (default N=3, configurable), or step budget exceeded, pauses the task for
   review within N+1 iterations, with an alert and audit event.
3. Frontend XSS hardening (TITAN-SEC-072): all LLM-derived content is
   untrusted — React default escaping only, an ESLint rule banning
   dangerouslySetInnerHTML for model content, markdown rendered through a
   rehype-sanitize allowlist pipeline, and CSP headers with default-src
   'self' and no unsafe-inline scripts served on every dashboard response.

Definition of done: a runaway-loop simulation halts at the cap within N+1
iterations with an alert; an org/month breach test pauses the task via the
existing budget_hold and raising the budget resumes it; a corpus of
stored-XSS payloads placed in agent output renders inert in the dashboard;
CSP headers are asserted by test and the ESLint ban fails a fixture
component. No TODOs in backend code.
```

---

### Phase 72 — Feature flags, kill switches & backpressure

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 14, 70

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: Phase 14 (worker process + run lifecycle pulling
tasks from Redis Streams); Phase 70 (rate limiting + validation middleware
with 429/Retry-After plumbing).

Read fully:
- Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §5.3 (feature
  flags, TITAN-OPS-012), §8.1–8.2 (HA design and the ledger-not-queue
  durability rule), and §9 (scaling model & backpressure, TITAN-OPS-020)

Build PHASE 72 — operational control surface:
1. Self-hosted feature flags (no SaaS): a feature_flags PostgreSQL table
   (flag key, description, type boolean/percentage/allowlist, scope
   global/org/project, state, owner, expiry date), cached in Redis with a
   10 s TTL and busted on write via pub/sub; exposed to the frontend via
   GET /v1/flags and to agents via the orchestrator context. Flag classes:
   release flags (default OFF), ops flags, experiment flags; every flag has
   an owner and expiry, and CI warns on flags older than 90 days.
2. Kill switches as ops flags — agents.pause_all, intake.read_only,
   llm.provider_override — effective within 15 seconds without any process
   restart (workers and the gateway re-check flags each dispatch/request
   cycle or subscribe to the pub/sub bust) (TITAN-OPS-012).
3. API admission control + backpressure (TITAN-OPS-020): per-project
   concurrent-task quota (default 25) returning 429 + Retry-After when
   exceeded; intake ALWAYS accepts and ledgers — every task is written to
   the PostgreSQL task ledger before enqueue, so Redis Streams is never the
   durability boundary; under saturation tasks wait in a visible "queued"
   state with honest status, streams are capped with XADD MAXLEN ~1M, an
   alert fires at oldest-pending > 300 s, and a
   ops/scripts/requeue-from-ledger.py script re-enqueues non-terminal
   ledger rows after queue loss.

Definition of done: flipping agents.pause_all in the API drops the
dispatch-rate metric to zero within 15 s with no restarts, and unflipping
resumes dispatch; a scripted k6 job driving intake at 5x worker capacity
(short local run) produces zero 5xx on intake endpoints and zero lost tasks
— every accepted request has a ledger row and the queue fully drains after
load stops; the requeue script restores a deliberately flushed queue from
the ledger. Tests for every new endpoint; no TODOs in backend code.
```

### Phase 73 — Audit chain + event-catalog instrumentation

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 68

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 68 (secrets vault with SOPS+age encryption, scoped
secret injection, field encryption, and signed message envelopes — reuse its age
keys for archive sealing).

Read fully: Project-TITAN-Docs/08-Volume-8-Security.md §10 (Audit Logging —
design, §10.2 event catalog, TITAN-SEC-090 through SEC-094).

Build PHASE 73 — audit chain + event-catalog instrumentation:
1. Append-only `audit_events` table in PostgreSQL: the application DB role gets
   INSERT-only grants (UPDATE/DELETE denied at the privilege level, not just in
   app code); every row carries the full SEC-090 schema {event_id, ts, actor,
   org, project, task, action, resource, outcome, source_ip, trace_id,
   event_hash} with event_hash = sha256(prev_hash || canonical_event_json) and
   trace_id linking to OpenTelemetry traces; a daily verification job walks the
   hash chain and alerts on any break (SEC-091).
2. Instrument every event class in §10.2 across the existing subsystems, through
   one shared audit client (no subsystem hand-rolls its own INSERTs):
   authentication success/failure/MFA/refresh/revocation/reuse-detection;
   authorization denials; role/member changes; secret lifecycle (never values);
   agent task lifecycle; every tool call with agent, tool, parameter digest, and
   allow/deny; approval-token mint/consume; sandbox create/destroy/policy
   exception; deploys; plugin install/config; memory-tier promotions; data
   export/deletion; scan results; redaction hits; rate-limit and SSRF blocks;
   incident-mode toggles. Events carry references/digests only — never secret
   values, full prompts, or code bodies (SEC-092).
3. Stream audit events to Loki for search, and write daily sealed age-encrypted
   segments to object storage (MinIO). Implement SEC-093 retention as a tested
   job: 400 days online + 2 years sealed archive, org-configurable upward,
   downward only by Owner, with a legal-hold override.

No TODOs in backend code; tests for the audit client, the chain verifier, and
the retention job (run it against synthetic aged data).

Definition of done: a test proves UPDATE and DELETE on audit_events as the app
role fail at the DB layer; corrupting one stored row makes the daily chain
verification fail and raise an alert; a catalog spot-check test exercises every
§10.2 event class and asserts each produces a schema-valid audit row.
```

### Phase 74 — CI security pipeline + dependency gate + SECURITY.md

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 3, 23

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 3 (ci.yml quality gates: lint, typecheck, tests,
coverage, plus the Pydantic→TypeScript type bridge); 23 (web tools + caching
registry mirror + egress proxy — all sandbox installs already route through the
mirror).

Read fully: Project-TITAN-Docs/08-Volume-8-Security.md §11 (Supply Chain
Security and CI Scanning, TITAN-SEC-100..105), §12 (OWASP Compliance Mapping,
SEC-110), §13.2 (SECURITY.md intake and disclosure, SEC-121).

Build PHASE 74 — CI security pipeline + dependency gate + SECURITY.md:
1. Extend the Phase-3 ci.yml with the §11.1 security stages in this order:
   secret scan (gitleaks) → SAST (semgrep with the custom TITAN ruleset — no
   string SQL, no `sh -c`, no dangerouslySetInnerHTML, no unvalidated tool-arg
   use — plus bandit and ESLint security) → dependency audit (pip-audit,
   npm audit, osv-scanner) → image scan (Trivy: CVEs, misconfig, licenses) →
   SBOM (Syft, CycloneDX) → image signing (cosign keyless via CI OIDC). Merges
   block on any detected secret, high+ SAST finding, or high/critical
   vulnerability unless a recorded, expiring, Admin-approved exception exists
   (SEC-100); lockfile drift fails CI (SEC-102). Do not build a second pipeline
   — these are new stages in the existing workflow.
2. Agent-dependency gate (SEC-104), enforced inside the Phase-23 registry
   mirror so there is exactly one enforcement and audit point (SEC-105): before
   any agent-proposed package installs, verify (a) it exists on the official
   registry — kills hallucinated packages; (b) age ≥ 30 days AND weekly
   downloads ≥ 1000, OR Maintainer-allowlisted; (c) osv-scanner shows no
   high/critical vulnerabilities; (d) name is not within Levenshtein distance 1
   of a top-1000 package unless it IS that package; (e) version pinned and
   hash-locked. Failures block the install and open a review task; direct-
   registry installs must fail; mirror logs record every install.
3. security/compliance-map.yaml: the machine-readable OWASP Top 10 (2021) +
   OWASP LLM Top 10 (2025) traceability from §12, mapping each row to its
   TITAN-SEC requirement IDs and test IDs, with a CI consistency check that
   fails when a mapped test is removed (SEC-110). SECURITY.md at repo root
   (SEC-121): reporting channel, response SLAs, 90-day coordinated disclosure,
   supported-versions policy — linked from the README.

Definition of done: a planted secret, a seeded vulnerable dependency, and a
typosquat package each block CI at their correct stage; a hallucinated
(nonexistent) package is rejected at the mirror while a legitimate package
passes; SECURITY.md exists and the README links to it.
```

### Phase 75 — Incident lockdown + red-team harness + security check CLI

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 72, 73, 74

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 72 (feature flags + kill switches: agents.pause_all,
intake.read_only, llm.provider_override, effective ≤ 15 s); 73 (tamper-evident
audit chain instrumented across all subsystems); 74 (CI security pipeline +
security/compliance-map.yaml).

Read fully: Project-TITAN-Docs/08-Volume-8-Security.md §13 (Incident Response
Runbook, SEC-120/121), §14 (Security Testing, SEC-130/131), §15 (Secure
Defaults Checklist, SEC-140), plus §12 for the SEC-110 compliance map you will
drive the CLI from.

Build PHASE 75 — incident lockdown + red-team harness + security check CLI:
1. `titan incident lockdown` command implementing §13.2 containment in ≤ 60
   seconds: pause all agent execution (flip the Phase-72 agents.pause_all
   switch), freeze deploys, revoke agent service tokens, and force privileged
   re-authentication — plus targeted variants (revoke a token family, kill a
   sandbox, disable a plugin, rotate a secret). Every toggle lands in the
   Phase-73 audit chain. Ship the four AI-specific playbooks from §13.3 as
   executable documents: P1 prompt-injection (quarantine content, replay task
   trace by trace_id, revert artifacts, add exploit to red-team corpus), P2
   malicious generated code merged, P3 secret in LLM context, P4 memory
   poisoning (SEC-120).
2. LLM red-team harness (SEC-130): a corpus of injection and tool-abuse
   payloads seeded from public suites (garak/PyRIT) plus TITAN-specific cases —
   injected README, malicious issue text, poisoned memory record, hostile web
   page for the Research Agent — run weekly in CI against a staging
   orchestrator with the real gateway and mock destructive tools; publishes a
   scorecard and blocks release on regression. Maintain a findings register
   where every incident and pen-test finding becomes a permanent regression
   test with traceability from finding to test ID (SEC-131).
3. `titan security check` CLI (SEC-140): verifies all 15 rows of the §15
   secure-defaults checklist (sandbox egress deny-all; auto-deploy/auto-merge
   OFF; loopback/internal binding; first-admin forced password change + MFA
   prompt; TLS + HSTS; JWT RS256/EdDSA 15-min expiry + refresh rotation;
   SOPS+age vault + secret-file exclusion; redaction on gateway/logs/sandbox
   sinks; rate limits + budget caps on; audit INSERT-only + hash chain active;
   non-root read-only containers; generated-project security gates on;
   per-project memory namespaces; generic errors + correlation IDs;
   auth-protected observability endpoints), prints a scorecard, and exits
   non-zero on any failure; row definitions come from compliance-map.yaml and
   CI runs the check against the Compose profile.

Definition of done: the full red-team corpus run reports zero unauthorized tool
executions and zero secret-bearing gateway payloads; a lockdown drill halts all
agent execution in ≤ 60 s (dispatch-rate metric drops to zero, audit records
the toggle); `titan security check` exits non-zero on a seeded
misconfiguration and green on the default install.
```

### Phase 76 — CI/CD release pipelines, signing & migration-compat gate

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 3, 74

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 3 (ci.yml PR pipeline: lint → typecheck → tests →
build); 74 (security stages already in ci.yml: gitleaks, semgrep/bandit, dep
audits, Trivy, Syft SBOM, cosign).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §3
(CI/CD Pipeline Specification, TITAN-OPS-004/005), §5.4 (Database Migrations in
Deployment, TITAN-OPS-013), §8.2 (backup schedule — nightly.yml hooks its
backup-verify job here).

Build PHASE 76 — CI/CD release pipelines, signing & migration-compat gate:
1. Three new workflows per §3.2 in .github/workflows/: main.yml (on merge to
   main: full pipeline, publish images to ghcr.io/titan-platform/<service>
   tagged sha-<short>, deploy staging, run smoke tests); release.yml (on tag
   v*: retag staged digests, conventional-commits changelog, GitHub Release,
   required-reviewer approval in a GitHub Environment, and the §3.4 mechanical
   gates — digest served ≥ 2 h in staging, staging error-rate SLO not breached
   during soak, E2E green on that digest — enforced by a pre-deploy job, with
   open SEV-1/SEV-2 + human approval as the manual gate); nightly.yml (full
   E2E on staging, k6 15-minute load profile, Trivy re-scan of deployed digests
   to catch newly disclosed CVEs, backup-verification job stub for Phase 78).
   Branch protection on main requires the named checks with no admin bypass
   (OPS-004); monorepo path filtering via dorny/paths-filter, full suite in the
   merge queue.
2. Signing and pinning discipline (OPS-005): all five images (titan-frontend,
   titan-api, titan-orchestrator, titan-worker, titan-migrate) built from
   digest-pinned bases, multi-arch, cosign keyless-signed via CI OIDC with Syft
   SBOMs attached; deploy manifests reference @sha256: digests only — never
   :latest or floating tags.
3. Expand→migrate→contract migration tooling per §5.4: the migrate job takes a
   Postgres advisory lock (one migrator at a time); backfills batch ≤ 5,000
   rows per transaction with sleeps; statements needing ACCESS EXCLUSIVE > 2 s
   are forbidden in standard releases; alembic downgrade scripts exist and are
   CI-tested. Add the OPS-013 previous-release compatibility gate: a CI job
   boots the previous release's images against the post-migration schema and
   runs the smoke suite green before any production migration is approved.

This phase is incremental over the Phase-3 ci.yml and the Phase-74 security
stages — extend them, state in each workflow exactly what it adds, and do not
duplicate existing stages into a second pipeline.

Definition of done: ci.yml runs green via act, and seeded failures (lint error,
type error, failing test, vulnerable dep) each stop the pipeline at the correct
stage; published images carry verifiable cosign signatures and SBOMs and no
floating tag appears in any manifest; a deliberately breaking migration turns
the migration-compat CI job red.
```

### Phase 77 — Grafana dashboards-as-code + SLO alerting

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 4, 17, 30

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 4 (observability foundation in Compose: Prometheus,
Grafana, Loki, otel-collector, standard RED/USE metrics + exporters); 17
(gateway cost accounting: per-call token/cost ledger in Postgres, budget caps);
30 (failure-recovery ladder + agent KPI emission).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md
§6.1-6.2 (Prometheus Metrics + Grafana Dashboards, TITAN-OPS-014/015) and §7
(SLOs & Alerting, TITAN-OPS-017).

Build PHASE 77 — Grafana dashboards-as-code + SLO alerting:
1. The remaining custom metrics/exporters so all 10 §6.1 agent-level metrics
   exist, labeled {agent_role, project_id, provider, model} with cardinality
   guarded (project_id capped to active projects, never per-task labels):
   titan_agent_tasks_total{status}, titan_agent_task_duration_seconds,
   titan_agent_tokens_total{direction}, titan_llm_cost_usd_total,
   titan_llm_request_duration_seconds, titan_llm_errors_total{kind},
   titan_queue_depth{stream,group}, titan_queue_oldest_pending_seconds,
   titan_project_budget_remaining_usd, titan_agent_review_rejections_total.
   Token and cost counters increment at the gateway as the single choke point
   and must reconcile with the Phase-17 Postgres cost ledger within 1%
   (OPS-014).
2. The five Grafana dashboards as JSON in ops/grafana/dashboards/ (no
   click-ops), provisioned as identical files for local, staging, and prod
   (OPS-015): Platform Overview (rate/error/latency p50/p95/p99 per service,
   restarts, deploy annotations from CI); Agent Operations (task throughput by
   role, duration heatmap, queue depth + oldest-pending age, retry/failure
   rates, active agents); LLM Spend & Providers (cost per hour/day by
   provider+model+project, token burn rate, budget-remaining table with
   green/amber/red thresholds, provider error/latency comparison,
   local-vs-cloud routing ratio); Data Stores (Postgres connections/lag/slow
   queries, Redis memory/ops/evictions — must be 0 on the queue DB, Qdrant RAM
   + search latency); SLO/Error Budget (burn-rate gauges per SLO, 28-day
   budget remaining).
3. SLO recording rules for the §7 table (SLO-API-AVAIL 99.9%, SLO-API-LAT p95
   < 500 ms, SLO-WS, SLO-TASK, SLO-GW, SLO-DUR) plus the 11 alert rules in
   ops/prometheus/rules/: APIErrorBudgetFastBurn (14.4× burn over 1 h AND
   5 m), APIErrorBudgetSlowBurn (3× over 6 h AND 30 m), QueueBackpressure,
   WorkerCrashLoop, PostgresReplicationLag, RedisQueueMemoryHigh,
   LLMProviderDegraded, ProjectBudgetNearExhaustion, GlobalSpendAnomaly,
   BackupFailed, CertExpirySoon — multiwindow multi-burn-rate for
   availability/latency SLOs, never static thresholds (OPS-017). Alertmanager
   routing: severity=page → ntfy (self-hosted)/PagerDuty, severity=ticket →
   GitHub issue via webhook; every paging alert carries a runbook_url
   annotation.

Definition of done: on a fresh `docker compose --profile obs up` all five
dashboards render from provisioning with no manual import; a synthetic SLO
breach (injected 5xx) fires the fast-burn alert within minutes and the alert
carries its runbook_url; all 10 custom metrics appear scraped in Prometheus.
```

### Phase 78 — Backups, restore, runbooks & production readiness

**Milestone:** M7 — Hardened & production-ready · **Size:** M · **Depends on:** 76, 77

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 76 (main/release/nightly workflows, image signing,
migration-compat gate); 77 (five Grafana dashboards + 11 alert rules including
BackupFailed, runbook_url annotations on every page).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §8.2
(Backup Schedule), §8.3 (Restore Procedure & RPO/RTO, TITAN-OPS-018), §11
(Production Readiness Checklist, TITAN-OPS-024), §12 (Operational Runbooks,
TITAN-OPS-025), and §3 for TITAN-OPS-006 (staging auto-deploy, tagged +
approved production deploys) which the deploy runbook operationalizes.

Build PHASE 78 — backups, restore, runbooks & production readiness:
1. Backups per the §8.2 schedule, all encrypted: PostgreSQL continuous WAL
   archiving to MinIO plus daily 02:00 UTC pg_basebackup (base 30 d, WAL 7 d
   PITR window, RPO ≤ 5 min); Qdrant collection snapshots every 6 h (14 d);
   Redis hourly RDB (48 h) — implemented as a Compose backup sidecar that
   ships ON by default. Restore scripts under ops/scripts/ including
   requeue-from-ledger.py (rebuild the Redis queue from non-terminal Postgres
   task-ledger rows — queue contents are not the durability boundary), and a
   nightly backup-verify job wired into the Phase-76 nightly.yml that restores
   the latest base+WAL into a scratch database and runs integrity checks,
   firing the Phase-77 BackupFailed page on failure or missed schedule
   (OPS-018).
2. A single make target, `make pitr-drill`, that drives the full round trip
   with no manual steps: take a backup → destroy the database → PITR-restore
   to a target time → validate task-ledger consistency → run the smoke suite.
3. The four runbooks in ops/runbooks/, one file each, all following the one
   skeleton (Preconditions → Steps with copy-pasteable commands → Verification
   → Escalation → Post-actions): RUNBOOK-01 deploy.md (release.yml trigger,
   green rollout, smoke on preview URL, approve flip, 30-min SLO watch);
   RUNBOOK-02 rollback.md (decision matrix, ops/scripts/rollback.sh, digest
   quarantine, 48-h post-mortem rule); RUNBOOK-03 incident.md (SEV
   definitions, roles, first-10-minutes flow, kill-switch inventory:
   agents.pause_all, intake.read_only, llm.provider_override); RUNBOOK-04
   backup-restore.md (single-table PITR-clone recovery, full Postgres PITR
   behind intake.read_only, Qdrant snapshot restore + re-embedding fallback,
   Redis ledger re-enqueue, full-DR sequence with expected timings). Every
   Phase-77 paging alert's runbook_url must resolve to the correct runbook
   anchor (OPS-025). Also ship ops/templates/prod-readiness.md: the §11
   checklist (build & supply chain, deploy, resilience, data, observability,
   security, cost, process) as a GitHub issue template requiring two
   maintainer sign-offs before first production launch, with a documented
   solo-operator mode for self-hosters (OPS-024).

Definition of done: `make pitr-drill` completes end-to-end and passes
integrity checks in one command; a scripted check proves every paging alert's
runbook_url resolves to an existing runbook anchor; the readiness checklist is
filled in with every item checked or marked as a documented gap with reason.
```

---

## M8 — Scale-out & evolution (optional) *(all phases optional, post-MVP)*

> **Milestone exit state:** K8s autoscaling under load; human-gated evolution proposals with bench gates

### Phase 79 — K8s base, resource governance & HA

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 78 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 78 (encrypted backups with automated restore verification, ops runbooks, and the production readiness checklist — backup artifacts and scripts exist under ops/).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §4 (Kubernetes Deployment Architecture) and §5 (Release Engineering), plus requirements TITAN-OPS-002, TITAN-OPS-005, TITAN-OPS-007, TITAN-OPS-008 and the §8.1 HA design.

Build PHASE 79 — Kubernetes base, resource governance & high availability:
1. K8s base + kustomize overlays in infra/k8s (base + overlays/staging + overlays/production),
   deploying the exact §4.1/§4.2 topology: Deployments for frontend, api, orchestrator, worker,
   and llm-gateway; StatefulSets with PVCs on SSD storage classes for PostgreSQL 16, Redis 7,
   and Qdrant (never exposed outside the cluster — TITAN-OPS-007); ClusterIP Services;
   ingress-nginx Ingress for app.titan.dev / api.titan.dev with cert-manager TLS and extended
   timeouts on /ws; the titan-migrate pre-deploy Job. Every pod gets liveness, readiness, and
   startup probes, with api/worker readiness gating on DB/Redis health so a bad config stalls
   the rollout instead of causing an outage (TITAN-OPS-008). Reuse the same container images as
   Docker Compose — env-based config only, per ADR-008; do not fork configuration.
2. Resource governance: the §4.4 requests/limits table applied verbatim (e.g. worker
   250m/1000m CPU, 512Mi/2Gi memory; postgres 1000m/2000m, 2Gi/4Gi), a namespace ResourceQuota
   on titan-prod, and a LimitRange injecting defaults so no pod ships without requests.
   All manifests are version-controlled in the monorepo, digest-pinned (never :latest), and
   linted with kubeconform in CI (TITAN-OPS-002).
3. HA per §8.1: PodDisruptionBudgets (minAvailable: 1; api: 2), podAntiAffinity +
   topologySpreadConstraints on all stateless tiers; PostgreSQL via the CloudNativePG operator
   with 1 primary + 1 streaming replica and automated failover; Redis primary/replica with 3
   Sentinels; Qdrant 2 replicas with collection replication factor 2; orchestrator active/
   passive with Postgres advisory-lock leader election (2 replicas, exactly one scheduling).
4. Kyverno admission policy enforcing TITAN-OPS-005: reject unsigned images (cosign keyless
   verification) and :latest tags. NetworkPolicies default-deny, permitting only the flows
   drawn in §4.1 — nightly-suite-style tests confirm datastore ports are unreachable from
   Ingress and from outside their allowed client set.

Extend the CI pipeline and backup scripts from earlier phases — add kubeconform and manifest
lint stages to the existing workflow, do not create a parallel pipeline. No TODOs in
manifests or scripts; every script must be idempotent.

Definition of done: kubeconform is green on base and all overlays; the full stack comes up
healthy on a local kind cluster; killing any singleton pod (CNPG primary, Redis master,
orchestrator leader) recovers automatically with no committed data loss, proven by a scripted
pod-kill test; a deploy referencing an unsigned image is rejected by the Kyverno admission
policy, proven by test.
```

### Phase 80 — Autoscaling + blue/green deployment

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 79 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 79 (K8s base + kustomize overlays, StatefulSets with CNPG/Sentinel HA, probes, PDBs, Kyverno image-signature admission — all healthy on kind).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §4.3–4.4 (Autoscaling, Resource Requests & Limits), §5.1–5.2 (Blue/Green Deployment, Rollback Strategy), §8.1 (HA Design), and requirements TITAN-OPS-009, TITAN-OPS-010, TITAN-OPS-011.

Build PHASE 80 — autoscaling and atomic blue/green releases:
1. HPA + KEDA autoscaling per the §4.3 table: frontend 2–6 on CPU 70%; api 3–12 on CPU +
   custom http_requests_in_flight (50 per pod); llm-gateway 2–8 on CPU + in-flight LLM calls
   (20 per pod); workers 2–50 via the KEDA Redis Streams scaler on consumer-group XPENDING
   lag, target 10 pending tasks per replica — queue depth, never CPU, because agent tasks are
   I/O-bound. Scale-down stabilization 300 s; workers drain gracefully (finish current task,
   ack, exit) within terminationGracePeriodSeconds: 120 (TITAN-OPS-009).
2. Blue/green machinery per §5.1: each stateless Deployment exists twice with a color label;
   the stable Service selects app=<svc>,color=<active>. A release job (a) applies vN+1 to the
   idle color, (b) waits for rollout and runs smoke tests against the color-specific preview
   Service, (c) patches the stable Service selector — an atomic, sub-second flip, (d) watches
   SLO burn for 30 minutes, then scales the old color to zero (TITAN-OPS-010). Workers flip by
   switching which color's consumer group is scaled; in-flight tasks on the old color drain.
3. Rollback: ops/scripts/rollback.sh <service|all> performing the selector flip in reverse
   (< 1 min while the old color is warm), plus automatic rollback triggered by a fast-burn SLO
   breach during the 30-minute post-flip window (TITAN-OPS-011). Database migrations are never
   rolled back — keep the §5.4 expand→migrate→contract discipline intact.

Extend the Phase 79 kustomize base with overlays for color and scaling — do not create a
second manifest tree. Reuse the existing fast-burn SLO alert rules from the observability
phases as the auto-rollback signal — do not define a second burn-rate calculation. No TODOs
in scripts; every script exits non-zero on failure.

Definition of done: a scripted k6 load test injecting 500 tasks scales workers up to plateau
and back down to min within 10 minutes of drain, with zero acked-task loss verified against
the Postgres task ledger; the blue/green flip is a single atomic selector patch (verified in
the release-job log); a staged chaos drill deploying a build with injected 5xx faults
auto-reverts without human action; manual rollback completes in under 5 minutes while the
previous color is warm.
```

### Phase 81 — DR kit, Terraform & NATS adapter

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 80 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 80 (HPA/KEDA autoscaling and blue/green releases with auto-rollback on the Phase 79 K8s base).

Read fully: Project-TITAN-Docs/09-Volume-9-Production-and-Operations.md §8.2–8.3 (Backup Schedule, Restore Procedure & RPO/RTO) and §9 (Scaling Model & Backpressure) with requirement TITAN-OPS-019; and Project-TITAN-Docs/01-Volume-1-Foundation-and-Product-Requirements.md ADR-005 (Redis 7 Streams at MVP; NATS JetStream as scale-out option) with requirement TITAN-FND-051.

Build PHASE 81 — disaster recovery kit, Terraform, and the NATS queue adapter:
1. DR drill kit: a scripted cluster-loss simulation that rebuilds the platform in a fresh
   kind/staging cluster following the §8.3 restore order exactly — (1) provision cluster from
   infra/ (Terraform + kustomize), (2) restore secrets, (3) restore Postgres via WAL PITR to
   latest, (4) start Redis empty and re-enqueue non-terminal ledger rows via
   ops/scripts/requeue-from-ledger.py, (5) restore Qdrant snapshots, (6) deploy last-known-good
   digests from the release manifest, (7) run the smoke suite, (8) flip DNS. The drill writes a
   report into ops/dr-drills/ recording measured RTO/RPO against the targets RTO ≤ 4 h /
   RPO ≤ 5 min (TITAN-OPS-019).
2. Terraform modules under infra/terraform: cluster + node pools (including the optional
   GPU pool tainted for the Ollama pool), S3-compatible object storage buckets for the §8.2
   backup targets, and DNS — cloud-agnostic module boundaries so no single vendor is baked
   into core (same images and env-based config promote unchanged).
3. NATS JetStream QueueBus adapter implementing the existing broker-agnostic QueueBus
   protocol (ADR-005: consumer groups, redelivery, and ordering semantics map from Redis
   Streams; this is the documented path past ~5,000 tasks/min per §9). The swap must be
   config-only, satisfying TITAN-FND-051 (scale to ≥ 100 concurrent tasks across ≥ 20 projects
   by adding workers, queue swap allowed).

Implement the adapter against the QueueBus interface built in the events-infrastructure
phase — do not fork a second queue abstraction, and do not modify the conformance suite to
fit NATS; NATS must fit the protocol. No TODOs in adapter code.

Definition of done: the scripted DR drill restores the full stack from backups with the smoke
suite green in the restored cluster and measured RTO/RPO logged in ops/dr-drills/; terraform
validate and plan run clean; the existing QueueBus conformance suite passes green on NATS,
including at-least-once redelivery and idempotent-consumption cases.
```

### Phase 82 — Extra connectors: GitLab, Jira, Linear, Figma

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 54 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 54 (GitHub, Slack & Discord first-party connectors running on the plugin runtime, with the GitHub reference connector covering the full PR lifecycle and signed webhooks).

Read fully: Project-TITAN-Docs/07-Volume-7-Integrations-and-Tool-Ecosystem.md §6.2 (GitLab), §6.4 (Jira & Linear sync), §6.5 (Figma read-only), plus §3.1 and TITAN-INT-011 for the plugin/manifest rules.

Build PHASE 82 — the extra connectors, each as a first-party plugin in plugins/first_party/*:
1. GitLab connector: a mirror of the GitHub reference connector mapped to GitLab semantics —
   merge requests, pipelines, webhooks verified via X-Gitlab-Token. Register provider-specific
   tools (gitlab.open_mr, …) AND the generic vcs.* aliases (vcs.open_change_request,
   vcs.merge) so Volume 6 workflow definitions stay provider-neutral (TITAN-INT-041).
   Self-hosted GitLab CE is the recommended free/local test target.
2. Jira + Linear sync connectors: TITAN is the source of truth for agent-created tasks; sync
   is two-way for status and comments (a human Jira comment becomes a task.comment_added event
   visible to the PM agent). Store a per-project mapping table (TITAN task ⇄ Jira issue key /
   Linear issue id) with TITAN states todo/in_progress/review/done/blocked mapped to tracker
   workflow states via config_schema. Jira: REST v3 + OAuth 2.0 (3LO) or API token, webhooks
   for jira:issue_updated; Linear: GraphQL + webhook signatures. Conflict rule: last-writer-
   wins with audit trail; simultaneous transitions flag the task sync_conflict for PM-agent
   resolution — never silently overwrite (TITAN-INT-043 targets ≤ 60 s status parity).
3. Figma read connector: tools figma.get_file (node tree), figma.get_node_image (rendered PNG
   for vision models), figma.get_variables (design tokens → Tailwind theme mapping), and
   figma.get_comments; auth via personal access token or OAuth; strictly risk: read
   (TITAN-INT-044).

Build every connector purely via manifest + entrypoint on the Phase 52 plugin runtime with
tools registered through the Tool Bus and credentials in the secret store — zero
modifications to core packages (TITAN-INT-011); deleting any connector directory must leave
core tests green. No TODOs; integration tests for every contributed tool and webhook,
including a bad-signature 401 rejection per connector.

Definition of done: each connector installs via manifest only and all its tools are gated and
audited by the Tool Bus; the GitLab MR flow passes E2E (branch → MR → review → merge through
the vcs.* aliases with GitLab as the only VCS connector enabled); a sync test mirrors task
transitions to Jira/Linear fixtures and flags a seeded simultaneous-edit as sync_conflict;
Figma token extraction from a fixture file produces a tailwind.config.ts whose palette
matches the file's variables.
```

### Phase 83 — Evolution: artifact versioning, prompt registry & EP gate

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 51, 78 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 51 (derived artifacts, event replay & the mock-LLM end-to-end pipeline run); 78 (backups, restore verification, runbooks & production readiness).

Read fully: Project-TITAN-Docs/10-Volume-10-Future-and-Autonomous-Evolution.md §2 (The Evolution Safety Model, TITAN-EVO-001/002/003), §3 (Scorecards context), ADR-EVO-01 (Self-Modification via Git PRs with Human Approval), and §6.1 (Prompt Registry & Versioning, TITAN-EVO-015).

Build PHASE 83 — the human-gated evolution framework:
1. /evolution/** artifact versioning: prompts, routing policies, agent specs, and benchmark
   definitions live as files in the monorepo, changed only via pull request. Add the
   evolution_artifacts PostgreSQL table storing ONLY immutable references (Git commit SHA +
   path + content hash) — no mutable prompt text may exist in the database. The runtime loader
   resolves every evolution artifact by pinned SHA from /evolution/manifest.yaml
   (TITAN-EVO-002).
2. Prompt registry: move all agent prompts to /evolution/prompts/<agent>/<template>.md with
   YAML front-matter (id, semver, variables, model constraints, eval requirements). The LLM
   gateway resolves agent → prompt SHA from the pinned manifest, and every LLM call logs its
   prompt SHA into its OpenTelemetry span; the dashboard can show which prompt version served
   any task (TITAN-EVO-015). No prompt text is constructed outside the registry except dynamic
   context injection through declared variables.
3. Evolution Proposal (EP) framework per ADR-EVO-01: every change to a prompt, routing policy,
   agent spec, or guardrail is expressed as a Git PR against /evolution/**. Agents may open,
   benchmark, and comment on EPs; only a human holding the evolution:approve RBAC permission
   may merge. The orchestrator refuses to load any evolution artifact whose EP lacks an
   approved_by user ID and signed approval timestamp; approvals land in the audit log with
   actor, artifact hash, and diff link (TITAN-EVO-001). Each deployed EP carries a monitoring
   contract (KPI floors + observation window) wired to automatic revert per TITAN-EVO-003.

Migrate the 16 prompt templates from the existing prompt-composition engine into the registry
and make the composer resolve them via the SHA-pinned manifest — do not keep a second prompt
source. Add evolution:approve to the existing RBAC engine — do not build a parallel
permission check. No TODOs in backend code; tests for every new endpoint and loader path.

Definition of done: an integration test proves an unapproved prompt version is never selected
at runtime (orchestrator refuses the artifact); git revert of an evolution commit followed by
reload restores the prior prompt bytes byte-for-byte; every LLM call span carries a prompt
SHA, verified in a trace assertion test.
```

### Phase 84 — Scorecards, KPI rollup & degradation alerts

**Milestone:** M8 — Scale-out & evolution · **Size:** M · **Depends on:** 83 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 83 (/evolution/** artifact versioning with SHA-pinned loader, prompt registry with prompt SHA in every OTel span, and the human-gated EP framework).

Read fully: Project-TITAN-Docs/10-Volume-10-Future-and-Autonomous-Evolution.md §3 (Agent Self-Evaluation: Scorecards & KPIs) and §4 (Reflection Loops context), requirements TITAN-EVO-004, TITAN-EVO-005, TITAN-EVO-006, TITAN-EVO-007.

Build PHASE 84 — agent scorecards, KPI rollups, and degradation alerts:
1. agent_scorecards table exactly per the TITAN-EVO-005 SQL schema (task_id, agent_id,
   agent_spec_sha, prompt_sha, model_id, task_class, dimensions JSONB, composite_score,
   cost_usd, tokens_in/tokens_out, wall_clock_s, trace_id; index on agent_id, task_class,
   created_at) as an Alembic migration. Every task reaching a terminal state (done, rejected,
   abandoned) triggers scorecard generation by the orchestrator across the 8 dimensions
   (correctness, first-pass quality, rework ratio, efficiency, cost, standards compliance,
   security, collaboration), combining objective telemetry (QA/CI results, review cycle count,
   diff churn, OTel spans, gateway metering, linters, security scans, message-bus analysis)
   with a rubric-based LLM evaluation by the Code Reviewer Agent for code tasks or the Project
   Manager Agent for non-code tasks — never by the agent that did the work. Composite weights
   per task class live in /evolution/scoring/weights.yaml, versioned through the Phase 83
   /evolution/** PR flow.
2. Nightly KPI rollup: a Redis Streams consumer (evolution.kpi-rollup) computes per-agent,
   per-task-class aggregates over 7/30/90-day windows, exposed as Prometheus metrics
   titan_agent_kpi{agent,kpi,window} and via GET /api/v1/evolution/agents/{agent_id}/kpis,
   published to the Grafana dashboards and the frontend Agent Status view (TITAN-EVO-006).
3. Degradation alerts (TITAN-EVO-007): a Prometheus alert rule on a sustained KPI drop
   (> 10% vs. the trailing 30-day mean for 3 consecutive days) that auto-creates an
   evolution.investigation task assigned to the Memory Agent, carrying the KPI series
   snapshot, and notifies maintainers through the configured connector channels.

Pin agent_spec_sha and prompt_sha from the Phase 83 registry and trace_id from the existing
OTel spans — do not invent new identifiers. Hook scorecard generation into the existing
terminal-task transition path of the task state machine — do not add a second completion
listener. No TODOs in backend code; tests for every endpoint.

Definition of done: an integration test (mock LLM) shows a scorecard row for 100% of terminal
tasks within 60 seconds of closure, with rubric dimensions produced by a different agent than
the executor and each row linking task ID, agent ID, prompt SHA, model ID, and trace ID; KPI
rollup values reconcile with raw scorecards within ±0.5%; a seeded KPI drop fires the
degradation alert and creates the investigation task with the series snapshot attached.
```

### Phase 85 — Reflections, retrospectives, failure taxonomy & RCR

**Milestone:** M8 — Scale-out & evolution (optional) · **Size:** M · **Depends on:** 40, 83 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 40 (memory decay/curation plus the base reflection and
lesson APIs writing through the Volume 5 memory pipeline); 83 (versioned evolution
artifacts under /evolution/**, the prompt registry with pinned SHAs per
TITAN-EVO-015, and the human-gated Evolution Proposal (EP) PR flow per
TITAN-EVO-001/002).

Read fully: Project-TITAN-Docs/10-Volume-10-Future-and-Autonomous-Evolution.md —
§4 (Reflection Loops, TITAN-EVO-008/009/010) and §5 (Learning from Failures,
TITAN-EVO-011/012/013/014), plus the TITAN-EVO-015 prompt-SHA rules and the
TITAN-EVO-016 bench linkage referenced from §5.3.

Build PHASE 85 — reflections, retrospectives, failure taxonomy & RCR:
1. Post-task reflections (TITAN-EVO-008): after scorecard generation, the
   executing agent runs one bounded LLM call answering the four questions (goal /
   what worked / what failed / what to do differently) and emits the structured
   post_task_reflection record from §4.1 (kind, task_id, agent_id, scorecard_id,
   went_well, went_poorly, lesson, proposed_change, confidence). Persist it via
   the existing Volume 5 write path — Qdrant `reflections` collection with
   metadata in PostgreSQL. Bounded retries; a failed reflection is logged and
   never blocks task closure; reflection cost is capped at 2% of the task's LLM
   cost; target ≥95% of terminal tasks.
2. Post-project retrospectives (TITAN-EVO-009): when a project reaches released
   or cancelled, the Project Manager Agent gathers all scorecards, reflections,
   incidents, and decision logs and generates a retrospective within 24h,
   stored in the project repo under /docs/retrospectives/ and containing: KPI
   summary vs. targets, top 5 lessons, failure inventory with RCR links, reuse
   candidates promoted to organization memory, and ≥0 Evolution Proposals. The
   CEO Agent countersigns; disagreements are recorded, never overwritten. Store
   the summarized embedding in Qdrant `org_memory` with kind=retrospective.
3. Failure taxonomy (TITAN-EVO-011): create /evolution/taxonomy/failures.yaml
   with the fixed, versioned F-codes from §5.1 (F-REQ, F-CTX, F-GEN, F-STD,
   F-SEC, F-ORCH, F-TOOL, F-LLM, F-HUM, plus the F-UNK fallback). Every failure
   event — task rejections, agent-attributable CI failures, incidents,
   rollbacks, guardrail trips — is classified by the Memory Agent with a rubric
   prompt to exactly one primary code (+ optional secondaries), human-
   overridable in the dashboard; sustained F-UNK >5% triggers a taxonomy-update
   EP.
4. Root-Cause Records (TITAN-EVO-012): the root_cause_records table exactly as
   the §5.2 SQL defines it (failure_code, severity low|medium|high|critical,
   task_ids UUID[], summary, five_whys JSONB, contributing JSONB with
   prompt_sha/model_id/routing rule/tool versions, remediation JSONB, status
   open|remediated|accepted_risk). An RCR is created within 24h for any failure
   above the severity threshold or any signature recurring ≥3 times in 30 days,
   authored by the Memory Agent and reviewed by the agent owning the failing
   domain; RCRs are searchable from the Memory Explorer screen.
5. RCR↔EP linkage (TITAN-EVO-013): remediations that require behavior change
   become EPs through the existing EP flow, each referencing its motivating
   RCR(s); merged EPs automatically annotate their RCRs; recurrence of the same
   signature after remediation reopens the RCR and flags the EP as
   ineffective. High/critical RCRs must reach remediated or accepted_risk
   within 30 days, tracked on a Grafana panel. Pure-knowledge remediations are
   written to organization memory with no approval gate, per §5.3.

Extend the Phase-40 reflection APIs and the Volume 5 memory write path — do not
build a second memory pipeline. Express every behavior-change remediation
through the Phase-83 EP mechanism — do not invent a separate approval flow. No
TODOs in backend code; tests for every new endpoint and the classification
rubric path.

Definition of done: an integration test completes a demo task and project, then
retrieves the reflection and retrospective through the Memory Packet retrieval
pipeline (correct kind and citations); a database constraint plus test proves
every RCR carries exactly one valid F-* code from failures.yaml; a test creates
an RCR, links an EP, merges it, and asserts the RCR is auto-annotated and that
a recurring signature reopens it.
```

### Phase 86 — TITAN-Bench, A/B harness & debate protocol

**Milestone:** M8 — Scale-out & evolution (optional) · **Size:** M · **Depends on:** 84, 85 · **Optional (post-MVP)**

```
Continue PROJECT TITAN. Specs are in ./Project-TITAN-Docs/ (source of truth).
Phases already completed: 84 (agent_scorecards table, per-agent KPI rollups with
7/30/90-day windows, Prometheus KPI metrics and degradation alerts); 85 (post-
task reflections, retrospectives, F-* failure taxonomy, root_cause_records and
RCR↔EP linkage). Phase 83's /evolution/** artifact versioning, prompt registry
and human-gated EP PR flow are also in place.

Read fully: Project-TITAN-Docs/10-Volume-10-Future-and-Autonomous-Evolution.md —
§6 (Continuous Optimization: TITAN-EVO-016/017/018 and ADR-EVO-02) and §7 (AI
Debates: TITAN-EVO-021/022/023, ADR-EVO-03), plus TITAN-EVO-003 (automatic
rollback triggers) from §2.

Build PHASE 86 — TITAN-Bench, A/B harness & debate protocol:
1. TITAN-Bench framework (TITAN-EVO-016): a fixed, versioned suite of agent-
   performance tasks executed in disposable Docker sandboxes, structured into
   the six tracks from §6.2 (B-CODE hidden-test implementation tasks, B-REVIEW
   seeded-defect PRs, B-PLAN requirements→task-graph checks, B-ARCH rubric-
   judged design questions, B-SEC labeled vulnerable samples, B-REG regression
   cases generated from Phase-85 RCRs). `titan bench run --track all
   --manifest <sha>` produces a scored JSON + Markdown report. Ship the smoke
   subset now: ≤15 minutes, laptop-runnable against local Ollama models, with
   at least a few cases per track; suite content is versioned so score
   comparisons are only valid within a suite version.
2. Offline gate (TITAN-EVO-017, ADR-EVO-02): per-track non-inferiority margins
   in /evolution/bench/gates.yaml; CI attaches the bench report to every EP
   pull request and blocks the merge checklist until results are attached and
   pass gates. No EP may enter online A/B until it beats or matches the
   champion on the relevant tracks.
3. A/B harness (TITAN-EVO-018): shadow mode (default — challenger receives
   copies of real task inputs, outputs scored but never used, costs metered
   under the evolution budget, working on Docker Compose with a single worker)
   and canary mode (challenger serves a default 10% of task classes whitelisted
   in the EP). Assignment is deterministic per task (hash-based) and recorded
   on the scorecard; analysis is a sequential test (SPRT/Bayesian) against the
   EP's pre-registered success metrics; promote/abort recommendations are
   generated automatically but executed only through the human gate.
4. Automatic rollback (TITAN-EVO-003): every deployed evolution change carries
   a monitoring contract of KPI floors and an observation window; a floor
   breach reverts to the champion configuration without human action within 5
   minutes, emits a Prometheus alert and audit event, and opens an incident RCR.
5. Multi-model debate, config-gated OFF by default: trigger policy engine
   reading /evolution/debate/policy.yaml with the §7.1 triggers (high-stakes
   decision classes always debated, reviewer rejecting the same artifact twice,
   self-reported confidence <0.5 on a critical task, blast radius ≥N modules or
   a public API contract, bench ambiguity within the non-inferiority margin)
   and a per-project daily debate cap; the four-round protocol (Round 0 framing
   → Round 1 independent positions from 2–3 debaters on distinct models via the
   LLM gateway → Round 2 rebuttals, max 2 rounds → Round 3 judgment) producing
   an ADR-format Decision Record with dissent appendix stored in the project
   decision log and organization memory, transcript viewable in Live
   Conversations; the three judge protocols from §7.3 (single judge default,
   3-judge panel majority for architecture/security, human-in-the-loop
   mandatory for EP debates), with judge independence verified at convening
   time (judge model+prompt never equals any debater's; positions anonymized
   A/B/C). Single-model deployments fall back to distinct personas with
   temperature variation, flagged as such (TITAN-EVO-021/022/023).
6. Explicit deferrals: record plugin marketplace (§9), SSO/SCIM (TITAN-
   EVO-033), multi-tenancy (TITAN-EVO-034), and fine-tuning (§11, TITAN-
   EVO-038..041) as documented deferrals mapped to roadmap Phases 3–5 in the
   in-repo roadmap/status document — do not implement any of them.

Score bench runs and both A/B arms through the Phase-84 agent_scorecards and
KPI rollup — do not invent a separate metrics store. Route every debate and
bench model call through the existing LLM gateway, metered under the evolution
budget; rollback incidents open Phase-85 RCRs — no second incident mechanism.
No TODOs in backend code; tests for every new endpoint.

Definition of done: a CI test proves an EP PR with failing (or missing) bench
results cannot merge and one passing gates.yaml can; a chaos test deploys an
artificially degraded challenger and asserts automatic rollback to the champion
in under 5 minutes with the alert, audit event, and incident RCR emitted; with
the debate flag at its default (off) no debate can be convened, and enabling
the flag runs a fixture debate end-to-end producing the ADR with dissent
section and a stored transcript.
```

---

## Fix-It Prompt (use whenever a phase went wrong)

```
The last phase of PROJECT TITAN has problems. Before writing any new code:
1. Re-read the specification sections listed in that phase's prompt in
   ./Project-TITAN-Docs/TITAN-BUILD-PHASES.md and list every requirement it
   defines for what we just built.
2. Audit the current code against that list: for each requirement mark OK,
   BROKEN, or MISSING, with file references.
3. Show me the audit table first. Then fix everything BROKEN or MISSING, in
   dependency order, running the tests after each fix.
4. Never delete working functionality to make a test pass; fix the root cause.
Here is what I observed going wrong: <describe the problem here>
```

---

*Part of the PROJECT TITAN specification set — see `00-README-Master-Overview.md` for the volume index. This file supersedes `BUILD-PROMPTS.md`.*
