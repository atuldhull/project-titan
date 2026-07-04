# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 9 — Production, Deployment & Operations

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform Specification |
| **Volume** | 9 of 10 — Production, Deployment & Operations |
| **Doc ID** | TITAN-VOL-09 |
| **Requirement Prefix** | TITAN-OPS-NNN |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |

**Abstract.** This volume specifies how TITAN is built, shipped, and operated: the GitHub Actions CI/CD pipeline; the three canonical environments (local Docker Compose, staging and production Kubernetes); the Kubernetes deployment architecture with autoscaling and resource governance; blue/green releases with rollback, feature flags, and online database migrations; the observability stack (Prometheus, Grafana, Loki, OpenTelemetry) including agent-level metrics for tokens, cost, and task throughput; SLOs and alerting; high availability and disaster recovery with explicit RPO/RTO targets; the scaling and queue-backpressure model; LLM cost controls with per-project budgets and local-model fallback; a production readiness checklist; and the four core operational runbooks. It is written so that a DevOps engineer — or TITAN's own DevOps Agent — can implement it directly. Canonical stack decisions are owned and justified by Volume 1; this volume applies them.

---

## Table of Contents

- 1. Scope, Principles & Definitions
- 2. Environments
  - 2.1 Environment Matrix
  - 2.2 Local — Docker Compose
  - 2.3 Staging — Kubernetes
  - 2.4 Production — Kubernetes
- 3. CI/CD Pipeline Specification
  - 3.1 Pipeline Stages
  - 3.2 Workflow Definitions
  - 3.3 Image Build & Publish
  - 3.4 Deployment Gates
- 4. Kubernetes Deployment Architecture
  - 4.1 Cluster Topology (ASCII)
  - 4.2 Workloads, Services & Ingress
  - 4.3 Autoscaling (HPA)
  - 4.4 Resource Requests & Limits
- 5. Release Engineering
  - 5.1 Blue/Green Deployment
  - 5.2 Rollback Strategy
  - 5.3 Feature Flags
  - 5.4 Database Migrations in Deployment
- 6. Monitoring & Observability
  - 6.1 Prometheus Metrics (Platform & Agent-Level)
  - 6.2 Grafana Dashboards
  - 6.3 Loki Log Aggregation
  - 6.4 OpenTelemetry Tracing Across Agent Pipelines
- 7. SLOs & Alerting
- 8. High Availability & Disaster Recovery
  - 8.1 HA Design
  - 8.2 Backup Schedule
  - 8.3 Restore Procedure & RPO/RTO
- 9. Scaling Model & Backpressure
- 10. Cost Optimization & LLM Spend Controls
- 11. Production Readiness Checklist
- 12. Operational Runbooks (Outline)
- 13. Decision Records
- 14. Risks & Open Questions
- 15. Cross-References

---

## 1. Scope, Principles & Definitions

This volume owns everything between "code merged" and "value delivered reliably in production": CI/CD, environments, Kubernetes architecture, releases, observability, alerting, DR, scaling, and cost. It does not re-litigate stack choices (Volume 1), backend internals (Volume 3), or security controls (Volume 8).

**Operating principles:**

1. **Local-first parity.** Every production capability must have a free, local equivalent. A contributor on one laptop with Docker Compose and Ollama must be able to run the entire platform, including the observability stack, at zero cost. This is a hard project requirement.
2. **Boring deploys.** Deployments are frequent, automated, reversible, and observable. A deploy that cannot be rolled back in under 5 minutes is a defect.
3. **Agents are workloads.** TITAN's AI agents are ordinary Kubernetes workloads with metrics, budgets, and autoscaling — not magic. Token spend is a first-class operational metric alongside CPU and memory.
4. **Everything as code.** Kubernetes manifests, dashboards, alert rules, and runbooks live in the monorepo under `infra/` and `ops/`; changes flow through the same PR + review process as application code.

**Definitions.** *Deploy* — replacing running workload versions. *Release* — exposing behavior to users (decoupled from deploy via feature flags). *RPO* — maximum tolerable data loss window. *RTO* — maximum tolerable restoration time. *Blue/green* — two identical production stacks where traffic switches atomically between them.

> **TITAN-OPS-001 (MUST)** — The complete platform, including observability, MUST run on a single developer laptop via `docker compose up` using only free/local tools (Ollama for models).
> *Acceptance:* Fresh clone → `cp .env.example .env` → `docker compose up` yields a working dashboard, one completed agent task via Ollama, and Grafana at `localhost:3001` — with no paid credentials.

> **TITAN-OPS-002 (MUST)** — All infrastructure (K8s manifests, Compose files, dashboards, alert rules) MUST be version-controlled in the monorepo and changed only via pull request.
> *Acceptance:* `kubectl diff -k infra/k8s/overlays/production` against the live cluster shows no drift after each deploy; CI fails on manifest lint errors.

---

## 2. Environments

### 2.1 Environment Matrix

| Property | Local | Staging | Production |
|---|---|---|---|
| Platform | Docker Compose | Kubernetes (small cluster or k3s) | Kubernetes |
| Namespace | n/a | `titan-staging` | `titan-prod` |
| LLM provider | Ollama (default) | Cheap cloud tier + Ollama | Configured per org; gateway-routed |
| Data | Seeded fixtures | Anonymized synthetic data | Real data |
| TLS | None (localhost) | Let's Encrypt staging CA | Let's Encrypt / org CA |
| Deploy trigger | Manual | Auto on merge to `main` | Manual approval on tagged release |
| Replicas | 1 each | 1–2 each | Per Section 4.4 |
| Backups | None | Weekly (retention 7 days) | Per Section 8.2 |

> **TITAN-OPS-003 (MUST)** — Staging MUST run the same images (identical digests) later promoted to production; only configuration (env, secrets, replicas) may differ.
> *Acceptance:* The production deploy job refuses any image digest not previously deployed to staging (verified via the release manifest).

### 2.2 Local — Docker Compose

`docker-compose.yml` at the repo root defines: `frontend` (Next.js 15 dev server), `api` (FastAPI + Uvicorn, hot reload), `orchestrator` (agent scheduler), `worker` (agent executor, scalable via `--scale worker=N`), `postgres:16`, `redis:7`, `qdrant`, `ollama`, plus an observability profile (`--profile obs`) with `prometheus`, `grafana`, `loki`, `promtail`, and `otel-collector`. All services share one bridge network; volumes persist Postgres, Qdrant, and Ollama model data.

### 2.3 Staging — Kubernetes

Staging is a scaled-down replica of production in the `titan-staging` namespace (or a separate small/k3s cluster for cost). Every merge to `main` auto-deploys here. Staging runs the full E2E suite nightly and is the mandatory soak environment before production promotion (minimum 2 h with synthetic agent workload).

### 2.4 Production — Kubernetes

Production runs in the `titan-prod` namespace with the topology of Section 4. Access is restricted per Volume 8 RBAC; humans get read-only `kubectl` by default, with break-glass write access audited and time-boxed.

---

## 3. CI/CD Pipeline Specification

### 3.1 Pipeline Stages

The canonical pipeline is GitHub Actions. Stage order is fixed: **lint → typecheck → tests → build → scan → publish images → deploy**.

```
                    GITHUB ACTIONS PIPELINE (per PR / per merge / per tag)
 ┌──────┐  ┌───────────┐  ┌─────────────────┐  ┌───────┐  ┌──────┐  ┌─────────┐  ┌────────┐
 │ lint │→ │ typecheck │→ │      tests      │→ │ build │→ │ scan │→ │ publish │→ │ deploy │
 └──────┘  └───────────┘  │ unit│integ│e2e* │  └───────┘  └──────┘  └─────────┘  └────────┘
  ruff       mypy strict  └─────────────────┘   docker     trivy      GHCR         staging: auto
  eslint     tsc --noEmit  pytest + coverage    buildx     (image)    multi-arch   prod: manual
  hadolint                 vitest / playwright  (cached)   pip-audit  sbom         approval +
  yamllint                 *e2e on main only               npm audit  (syft)       blue/green
```

| Stage | Tools | Fail condition |
|---|---|---|
| lint | `ruff check`, `ruff format --check`, `eslint`, `prettier --check`, `hadolint`, `yamllint`, `kubeconform` on manifests | Any violation |
| typecheck | `mypy --strict` (backend), `tsc --noEmit` (frontend) | Any type error |
| tests | `pytest` (unit + integration w/ Postgres+Redis service containers), `vitest`; Playwright E2E on `main` merges and release tags | Failure, or backend coverage < 80% / frontend < 70% |
| build | `docker buildx` multi-stage builds for `api`, `orchestrator`, `worker`, `frontend`, `migrate`; layer cache via GHA cache | Build error |
| scan | `trivy image` (CRITICAL/HIGH block), `pip-audit`, `npm audit --audit-level=high`, `gitleaks` (secrets) | Any blocking finding (see Volume 8 for triage/waiver process) |
| publish | Push to GHCR `ghcr.io/titan-platform/<service>` tagged `sha-<short>` and `vX.Y.Z` on tags; SBOM attached via `syft`; images signed with `cosign` keyless | Push/sign failure |
| deploy | `kubectl apply -k` via OIDC-federated service account (no long-lived cloud keys) | Rollout not healthy in 10 min |

> **TITAN-OPS-004 (MUST)** — Every PR MUST pass lint, typecheck, unit + integration tests, and dependency/secret scans before merge; branch protection enforces this with no admin bypass.
> *Acceptance:* GitHub branch protection on `main` requires the named checks; a PR with a failing check cannot be merged.

> **TITAN-OPS-005 (MUST)** — Container images MUST be immutable, digest-addressed, signed (cosign), and carry an SBOM; deploy manifests MUST pin digests, never floating tags.
> *Acceptance:* Kyverno/Gatekeeper policy in-cluster rejects unsigned images and `:latest` tags; `kubectl get deploy -o yaml` shows `@sha256:` digests only.

> **TITAN-OPS-006 (MUST)** — Merge to `main` MUST auto-deploy to staging; production deploys MUST require a signed git tag `vX.Y.Z` plus manual approval in a GitHub Environment with required reviewers.
> *Acceptance:* Audit log shows approver identity for every production deploy; untagged commits cannot reach production.

### 3.2 Workflow Definitions

Workflows live in `.github/workflows/`:

- `ci.yml` — PR pipeline (lint → typecheck → tests → build → scan; no publish).
- `main.yml` — on merge to `main`: full pipeline, publish `sha-*` images, deploy staging, run smoke tests, run nightly-tier E2E.
- `release.yml` — on tag `v*`: retag staged digests, generate changelog (conventional commits), create GitHub Release, await approval, execute blue/green production deploy (Section 5.1).
- `nightly.yml` — full E2E on staging, load test (k6, 15-minute profile), Trivy re-scan of *deployed* digests (catches newly disclosed CVEs), backup verification job (Section 8.3).

Monorepo path filtering (`dorny/paths-filter`) skips unaffected jobs (frontend-only PRs skip backend tests) but the merge queue always runs the full suite.

### 3.3 Image Build & Publish

Five images, one `Dockerfile` per service under `infra/docker/`: `titan-frontend` (Next.js standalone output, node:22-slim, non-root), `titan-api`, `titan-orchestrator`, `titan-worker` (all three from a shared python:3.12-slim base stage with uv-installed dependencies, non-root, read-only root FS), and `titan-migrate` (Alembic + migration scripts only). Multi-arch: `linux/amd64,linux/arm64`.

### 3.4 Deployment Gates

Production promotion requires, in order: (1) digest served ≥ 2 h in staging; (2) staging error-rate SLO not breached during soak; (3) E2E suite green on that digest; (4) no open SEV-1/SEV-2 incident; (5) human approval. Gates 1–3 are enforced mechanically by the `release.yml` pre-deploy job querying Prometheus and the CI API; gates 4–5 are the approval step.

---

## 4. Kubernetes Deployment Architecture

### 4.1 Cluster Topology (ASCII)

```
                                  INTERNET
                                     │
                          ┌──────────▼──────────┐
                          │  Cloud LB / L4      │
                          └──────────┬──────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │  ingress-nginx (2 replicas)     │  TLS: cert-manager
                    │  app.titan.dev  api.titan.dev   │  + Let's Encrypt
                    └───────┬───────────────┬─────────┘
              namespace: titan-prod         │
        ┌───────────────────┴───┐   ┌───────┴────────────────────────┐
        │ Service: frontend     │   │ Service: api                   │
        │ Deployment: frontend  │   │ Deployment: api (FastAPI)      │
        │ replicas 2–6 (HPA)    │   │ replicas 3–12 (HPA)            │
        └───────────────────────┘   │ /ws WebSockets (sticky-less,   │
                                    │  Redis pub/sub fan-out)        │
                                    └───────┬────────────────────────┘
                                            │ internal ClusterIP traffic
      ┌─────────────────────────────────────┼──────────────────────────────┐
      │                                     │                              │
┌─────▼─────────────────┐   ┌───────────────▼────────┐   ┌─────────────────▼─────┐
│ Deployment:           │   │ Deployment: worker     │   │ Deployment:           │
│ orchestrator          │   │ (agent executors)      │   │ llm-gateway           │
│ replicas 2 (leader-   │   │ replicas 2–50 (HPA on  │   │ replicas 2–8 (HPA)    │
│ elected scheduler)    │   │ queue depth, KEDA)     │   │ → Anthropic/OpenAI/   │
└─────┬─────────────────┘   └──────┬─────────────────┘   │   Google/Ollama pool  │
      │                            │                     └───────────┬───────────┘
      │   ┌────────────────────────┼─────────────────────────────────┘
      │   │                        │
┌─────▼───▼────────┐  ┌────────────▼──────┐  ┌────────────────┐  ┌───────────────┐
│ PostgreSQL 16    │  │ Redis 7           │  │ Qdrant         │  │ Ollama pool   │
│ StatefulSet,     │  │ StatefulSet,      │  │ StatefulSet,   │  │ (optional GPU │
│ 1 primary +      │  │ primary + replica │  │ 2 replicas,    │  │  node pool,   │
│ 1 streaming      │  │ + Sentinel ×3     │  │ PVC (SSD)      │  │  taint: gpu)  │
│ replica (PVC)    │  │ (queues/cache/pub)│  └────────────────┘  └───────────────┘
└──────────────────┘  └───────────────────┘
      namespace: titan-observability
┌──────────────┐ ┌─────────┐ ┌──────┐ ┌────────────────┐ ┌──────────────┐
│ Prometheus   │ │ Grafana │ │ Loki │ │ otel-collector │ │ Alertmanager │
└──────────────┘ └─────────┘ └──────┘ └────────────────┘ └──────────────┘
```

### 4.2 Workloads, Services & Ingress

| Workload | Kind | Purpose | Service | Exposure |
|---|---|---|---|---|
| frontend | Deployment | Next.js 15 UI | ClusterIP | Ingress `app.titan.dev` |
| api | Deployment | FastAPI REST + WebSockets | ClusterIP | Ingress `api.titan.dev` (`/ws` with extended timeouts) |
| orchestrator | Deployment | Task graph scheduling, agent lifecycle | ClusterIP (metrics only) | Internal |
| worker | Deployment | Executes agent tasks from Redis Streams | Headless (metrics) | Internal |
| llm-gateway | Deployment | Provider-agnostic LLM routing, budgets, fallback | ClusterIP | Internal only |
| postgres | StatefulSet | System of record | ClusterIP | Internal; no Ingress ever |
| redis | StatefulSet ×3 + Sentinel | Queues, cache, pub/sub | ClusterIP | Internal |
| qdrant | StatefulSet | Vector memory | ClusterIP | Internal |
| titan-migrate | Job (Helm hook / pre-deploy) | Alembic migrations | n/a | n/a |

WebSocket connections terminate on any `api` pod; cross-pod event fan-out uses Redis pub/sub, so no session affinity is required (Volume 3 owns the eventing contract). NetworkPolicies default-deny; only the arrows in Section 4.1 are permitted flows (details in Volume 8).

> **TITAN-OPS-007 (MUST)** — All stateful services (PostgreSQL, Redis, Qdrant) MUST run as StatefulSets with PVCs on SSD storage classes and MUST NOT be exposed outside the cluster.
> *Acceptance:* NetworkPolicy tests (part of nightly suite) confirm datastore ports unreachable from Ingress and from outside their allowed client set.

> **TITAN-OPS-008 (MUST)** — Every pod MUST define liveness, readiness, and startup probes; API/worker readiness MUST gate on dependency health (DB, Redis) so rollouts halt on broken configuration.
> *Acceptance:* Deploying an image with an invalid DB DSN to staging results in a stalled rollout (old pods still serving), not an outage.

### 4.3 Autoscaling (HPA)

| Workload | Min | Max | Signal | Target |
|---|---|---|---|---|
| frontend | 2 | 6 | CPU | 70% |
| api | 3 | 12 | CPU + `http_requests_in_flight` (custom) | 70% / 50 in-flight per pod |
| worker | 2 | 50 | KEDA: Redis Streams pending entries (`XPENDING` lag per consumer group) | 10 pending tasks per replica |
| llm-gateway | 2 | 8 | CPU + in-flight LLM calls | 70% / 20 per pod |

Workers scale on **queue depth, not CPU** — agent tasks are I/O-bound (waiting on LLM responses). KEDA's Redis Streams scaler reads consumer-group lag directly. Scale-down stabilization is 300 s to avoid flapping; workers drain gracefully (finish current task, ack, exit) within a 120 s `terminationGracePeriodSeconds`.

> **TITAN-OPS-009 (MUST)** — Worker autoscaling MUST be driven by Redis Streams consumer-group lag via KEDA, with graceful drain on scale-down (no task lost or double-executed beyond at-least-once semantics).
> *Acceptance:* Load test injecting 500 tasks scales workers to plateau and back down to min within 10 min of drain, with zero unacked-lost tasks (verified via task ledger in Postgres).

### 4.4 Resource Requests & Limits

| Workload | CPU req / lim | Mem req / lim | Notes |
|---|---|---|---|
| frontend | 100m / 500m | 256Mi / 512Mi | |
| api | 250m / 1000m | 512Mi / 1Gi | Uvicorn, 1 process/pod; scale via replicas |
| orchestrator | 250m / 1000m | 512Mi / 1Gi | |
| worker | 250m / 1000m | 512Mi / 2Gi | Memory head-room for large contexts |
| llm-gateway | 100m / 500m | 256Mi / 512Mi | Mostly I/O wait |
| postgres | 1000m / 2000m | 2Gi / 4Gi | Tune `shared_buffers` = 1Gi |
| redis | 250m / 1000m | 1Gi / 2Gi | `maxmemory 1536mb`, `noeviction` on queue DB |
| qdrant | 500m / 2000m | 1Gi / 4Gi | |

Namespace `ResourceQuota` caps `titan-prod` totals; a `LimitRange` injects defaults so no pod ships without requests. Memory limits are hard (OOMKill is a signal, alerted on); CPU limits are set generously to avoid throttling latency-sensitive paths.

---

## 5. Release Engineering

### 5.1 Blue/Green Deployment

Stateless tiers (frontend, api, orchestrator, worker, llm-gateway) deploy blue/green; datastores are shared across colors (hence the migration discipline in 5.4).

```
 STEP 1: green idle          STEP 2: deploy vN+1 to green    STEP 3: flip Service selector
 ┌────────┐                  ┌────────┐  ┌────────┐          ┌────────┐  ┌────────┐
 │BLUE vN │◄── Service       │BLUE vN │◄─┤ smoke  │          │BLUE vN │  │GREEN   │◄── Service
 │(live)  │    selector      │(live)  │  │ tests  │          │(idle,  │  │vN+1    │    selector
 └────────┘    color=blue    └────────┘  │ hit    │          │kept    │  │(live)  │    color=green
                             ┌────────┐  │ green  │          │30 min) │  └────────┘
                             │GREEN   │◄─┤directly│          └────────┘  STEP 4: after 30 min
                             │vN+1    │  └────────┘                      healthy → scale blue
                             └────────┘                                  to 0 (manifests kept)
```

Mechanics: each Deployment exists twice with a `color` label; the stable Service selects `app=<svc>,color=<active>`. The release job (1) applies vN+1 to the inactive color, (2) waits for rollout and runs smoke tests against the color-specific preview Service, (3) patches the stable Service selector — an atomic, sub-second flip; existing WebSockets drain within the 120 s grace window and clients auto-reconnect per Volume 4's contract, (4) watches SLO burn for 30 minutes, then scales the old color to zero. Workers flip by switching which color's consumer group is scaled up; in-flight tasks on the old color drain to completion.

> **TITAN-OPS-010 (MUST)** — Production deploys MUST use blue/green with automated smoke tests against the idle color before traffic flip, and a 30-minute observation window before decommissioning the previous color.
> *Acceptance:* Release pipeline logs show smoke-test pass on the preview endpoint, timestamped flip, and old color retained ≥ 30 min for every production release.

### 5.2 Rollback Strategy

Rollback is the selector flip in reverse and completes in under one minute while the old color is warm. Triggers: automatic when the post-flip watch detects SLO fast-burn (Section 7) within the 30-minute window; manual anytime via `ops/scripts/rollback.sh <service|all>`. After the old color is decommissioned, rollback = redeploy of the previous digest through the same blue/green flow (target < 15 min). Database migrations are **never** rolled back in production (see 5.4); code must roll back cleanly against the newer schema.

> **TITAN-OPS-011 (MUST)** — Automated rollback MUST trigger on fast-burn SLO breach during the 30-minute post-flip window; manual rollback MUST complete in < 5 minutes while the previous color is warm.
> *Acceptance:* Chaos drill (mandatory per release-candidate cycle, staged in staging): deploy a build with injected 5xx faults; system auto-reverts without human action and pages the on-call.

### 5.3 Feature Flags

TITAN self-hosts flags (local-first; no SaaS dependency): a `feature_flags` table in PostgreSQL (flag key, description, type: boolean/percentage/allowlist, scope: global/org/project, state, owner, expiry date), cached in Redis with a 10 s TTL and busted on write via pub/sub. The API exposes flags to the frontend via a `/v1/flags` endpoint and to agents via the orchestrator context. Admin UI lives in the dashboard's Settings area (Volume 4).

Flag classes: **release flags** (new features ship dark, flipped after deploy — decoupling deploy from release), **ops flags** (kill switches: `agents.pause_all`, `llm.provider_override`, `intake.read_only`), and **experiment flags** (percentage rollouts). Every flag has an owner and an expiry; CI warns on flags older than 90 days.

> **TITAN-OPS-012 (MUST)** — Risky changes (new agent behaviors, new external integrations, schema-adjacent logic) MUST ship behind release flags defaulting OFF; global kill switches `agents.pause_all` and `llm.provider_override` MUST exist and take effect within 15 seconds.
> *Acceptance:* Flipping `agents.pause_all` in the admin UI halts new task dispatch across all workers within 15 s (verified by dispatch-rate metric dropping to zero) without pod restarts.

### 5.4 Database Migrations in Deployment

Migrations are Alembic scripts run by the `titan-migrate` Job **before** the new color receives traffic, under the **expand → migrate → contract** discipline:

1. **Expand (release N):** additive-only DDL — new tables, nullable columns, new indexes built `CONCURRENTLY`. Old and new code both work.
2. **Migrate (release N or N+1):** backfill data in batches (≤ 5,000 rows per transaction, sleep between batches); dual-write behind a flag if needed.
3. **Contract (release N+2 at the earliest):** drop old columns/tables only after no deployed code references them and a full backup cycle has passed.

Rules: the migrate Job takes a Postgres advisory lock (one migrator at a time); any statement needing `ACCESS EXCLUSIVE` for more than 2 s is forbidden in standard releases and requires a maintenance-window change record; `alembic downgrade` scripts must exist and be tested in CI (staging), but production recovery uses roll-forward or restore, never downgrade.

> **TITAN-OPS-013 (MUST)** — All production schema changes MUST follow expand→migrate→contract; each release's migrations MUST be backward-compatible with the previous release's code (blue/green requirement, since both colors share one database).
> *Acceptance:* CI job boots previous-release images against post-migration schema in staging and runs the smoke suite green before any production migration is approved.

---

## 6. Monitoring & Observability

All services emit OpenTelemetry (traces, metrics, logs) to a namespace-local `otel-collector`, which routes metrics to Prometheus (via the Prometheus exporter / remote-write), traces to Tempo (local/staging) or a configured OTLP backend, and enriches logs picked up by Promtail into Loki. In Docker Compose, the identical collector config runs as a container — full local parity.

### 6.1 Prometheus Metrics (Platform & Agent-Level)

Standard RED/USE metrics come from FastAPI middleware, `redis_exporter`, `postgres_exporter`, and `kube-state-metrics`. TITAN adds first-class **agent-level metrics** (all labeled `{agent_role, project_id, provider, model}` — cardinality guarded: project_id capped to active projects, never per-task labels):

| Metric | Type | Meaning |
|---|---|---|
| `titan_agent_tasks_total{status}` | counter | Tasks completed/failed/retried per agent role |
| `titan_agent_task_duration_seconds` | histogram | End-to-end task latency per role |
| `titan_agent_tokens_total{direction}` | counter | Prompt/completion tokens per role, provider, model |
| `titan_llm_cost_usd_total` | counter | Accrued spend (gateway computes from provider price table) |
| `titan_llm_request_duration_seconds` | histogram | Gateway-observed LLM latency per provider/model |
| `titan_llm_errors_total{kind}` | counter | Rate-limit / timeout / refusal / 5xx per provider |
| `titan_queue_depth{stream, group}` | gauge | Redis Streams pending entries per consumer group |
| `titan_queue_oldest_pending_seconds` | gauge | Age of oldest unacked task (backpressure signal) |
| `titan_project_budget_remaining_usd` | gauge | Per-project remaining LLM budget |
| `titan_agent_review_rejections_total` | counter | Code Reviewer Agent rejections (quality signal, feeds Volume 10) |

> **TITAN-OPS-014 (MUST)** — Every LLM call MUST increment token and cost counters at the gateway (single choke point), and every agent task MUST record duration and outcome; no agent may call a provider except through the gateway.
> *Acceptance:* Sum of `titan_llm_cost_usd_total` over a test window matches the gateway's Postgres cost ledger within 1%, and NetworkPolicy blocks worker egress to provider APIs directly.

### 6.2 Grafana Dashboards

Dashboards are provisioned as JSON in `ops/grafana/dashboards/` (no click-ops):

| Dashboard | Panels (minimum) |
|---|---|
| **Platform Overview** | Request rate/error rate/latency (p50/p95/p99) per service; pod restarts; HPA replica counts; deploy annotations from CI |
| **Agent Operations** | Task throughput by role; task duration heatmap; queue depth & oldest-pending age; retry/failure rates; active agents by role |
| **LLM Spend & Providers** | Cost per hour/day by provider+model+project; token burn rate; budget-remaining table (thresholds green/amber/red); provider error & latency comparison; local-vs-cloud routing ratio |
| **Data Stores** | Postgres connections/replication lag/slow queries; Redis memory/ops/evictions (must be 0 on queue DB); Qdrant RAM & search latency |
| **SLO / Error Budget** | Burn-rate gauges per SLO (Section 7), 28-day budget remaining |

> **TITAN-OPS-015 (MUST)** — The five dashboards above MUST be provisioned as code and identical (same JSON) in local, staging, and production Grafana.
> *Acceptance:* `docker compose --profile obs up` shows all five dashboards populated; a diff between local and prod provisioned JSON is empty.

### 6.3 Loki Log Aggregation

All services log structured JSON to stdout: `timestamp, level, service, trace_id, span_id, project_id, agent_role, task_id, message, attrs`. Promtail (DaemonSet) ships to Loki with labels `{namespace, service, level}` only (low cardinality; everything else is queried from the JSON body). Retention: 14 days local/staging, 30 days production hot + 90 days object-storage archive. Secrets/token values are redacted at the logging library level (Volume 8 owns the redaction list). Agent *conversation* transcripts are business data stored in PostgreSQL (Volume 5), not in Loki — logs carry only task/message IDs linking to them.

### 6.4 OpenTelemetry Tracing Across Agent Pipelines

A single trace follows a user request across the entire agent pipeline: `POST /v1/projects/{id}/requests` → orchestrator planning span → per-task spans (queued → assigned → executing) → nested LLM-call spans at the gateway → review/QA spans → deploy action spans. Context propagates via W3C `traceparent` headers on HTTP and an embedded `traceparent` field in every Redis Streams message envelope (workers resume the context on consume). LLM spans record `gen_ai.*` semantic-convention attributes (model, provider, token counts) but **never** prompt/completion bodies (PII/IP risk — bodies live in the Volume 5 memory store). Sampling: 100% local/staging; production tail-sampling keeps all error traces + 10% of successes.

> **TITAN-OPS-016 (MUST)** — Trace context MUST propagate through Redis Streams so one trace covers a request end-to-end (API → orchestrator → workers → LLM gateway); traces MUST NOT contain prompt or completion text.
> *Acceptance:* For a staged "build a TODO app" request, Tempo shows a single trace containing ≥ 1 orchestrator span, ≥ 3 agent task spans, and nested LLM spans, with zero prompt-body attributes.

---

## 7. SLOs & Alerting

SLOs are measured over rolling 28 days; error budgets drive release pace (budget exhausted → feature deploys pause, only fixes ship).

| SLO ID | Service objective | Target | SLI source |
|---|---|---|---|
| SLO-API-AVAIL | API success rate (non-5xx) | 99.9% | `http_requests_total` |
| SLO-API-LAT | API p95 latency (non-LLM endpoints) | < 500 ms | request histogram |
| SLO-WS | WebSocket event delivery within 2 s | 99.5% | synthetic probe |
| SLO-TASK | Agent task pickup ≤ 60 s of enqueue (at normal load) | 99% | `titan_queue_oldest_pending_seconds` |
| SLO-GW | LLM gateway availability (incl. fallback success) | 99.5% | gateway metrics |
| SLO-DUR | Durability: zero acknowledged-task loss | 100% | ledger reconciliation job |

Alertmanager routes: `severity=page` → PagerDuty/ntfy (self-hosted option) to on-call; `severity=ticket` → GitHub issue via webhook. Core rules (full set in `ops/prometheus/rules/`):

| Alert | Condition | Severity |
|---|---|---|
| APIErrorBudgetFastBurn | 14.4× burn rate over 1 h AND 5 m (multiwindow) | page |
| APIErrorBudgetSlowBurn | 3× burn over 6 h AND 30 m | ticket |
| QueueBackpressure | `titan_queue_oldest_pending_seconds > 300` for 10 m | page |
| WorkerCrashLoop | pod restarts > 3 in 15 m | page |
| PostgresReplicationLag | lag > 30 s for 5 m | page |
| RedisQueueMemoryHigh | queue DB memory > 80% for 10 m | page |
| LLMProviderDegraded | provider error rate > 20% over 10 m | ticket (auto-fallback handles) |
| ProjectBudgetNearExhaustion | `titan_project_budget_remaining_usd / budget < 0.1` | ticket + in-app notification |
| GlobalSpendAnomaly | hourly spend > 3× trailing 7-day hourly median for 2 h | page |
| BackupFailed | backup job non-zero exit or missed schedule | page |
| CertExpirySoon | TLS cert < 14 days to expiry | ticket |

> **TITAN-OPS-017 (MUST)** — SLO alerts MUST use multiwindow multi-burn-rate rules (not static thresholds) for availability/latency SLOs; every page MUST link to its runbook section.
> *Acceptance:* Alert definitions carry `runbook_url` annotations; synthetic fault injection in staging fires fast-burn within 5 minutes and the page contains the link.

---

## 8. High Availability & Disaster Recovery

### 8.1 HA Design

Every stateless tier runs ≥ 2 replicas with `podAntiAffinity` across nodes and `topologySpreadConstraints` across zones (where the cluster is multi-zone); `PodDisruptionBudgets` keep `minAvailable: 1` (api: 2) during node drains. PostgreSQL runs one primary + one streaming replica with automated failover via the operator (CloudNativePG); Redis runs primary/replica with 3 Sentinels; Qdrant runs 2 replicas with collection replication factor 2. The orchestrator is active/passive via Postgres advisory-lock leader election — two replicas, one scheduling. Kubernetes reschedules failed pods; the single-node local environment explicitly accepts no HA.

### 8.2 Backup Schedule

| Data | Method | Frequency | Retention | Encrypted |
|---|---|---|---|---|
| PostgreSQL | WAL archiving (continuous, to S3-compatible object storage — MinIO acceptable) + `pg_basebackup` | WAL: continuous; base: daily 02:00 UTC | Base: 30 d; WAL: 7 d (PITR window) | AES-256 at rest (Volume 8 KMS) |
| Qdrant | Collection snapshots to object storage | Every 6 h | 14 d | Yes |
| Redis | RDB snapshot to object storage | Hourly | 48 h | Yes |
| Object storage (artifacts, workspaces) | Cross-bucket replication | Continuous | 30 d versioning | Yes |
| Cluster state | `infra/` in git is source of truth; Velero for PVC/state safety net | Velero: daily | 14 d | Yes |
| Secrets | Sealed/external-secrets source of truth in KMS (Volume 8) | On change | n/a | Yes |

Redis queue contents are deliberately **not** the durability boundary: every task is recorded in the PostgreSQL task ledger before enqueue, so queue loss is recoverable by re-enqueueing non-terminal ledger rows (`ops/scripts/requeue-from-ledger.py`).

### 8.3 Restore Procedure & RPO/RTO

**Targets:** PostgreSQL RPO ≤ 5 min (WAL PITR), RTO ≤ 1 h. Qdrant RPO ≤ 6 h, RTO ≤ 2 h (degraded-but-operational sooner: agents run with reduced memory recall; Volume 5 defines re-embedding from Postgres source data as the true recovery of record). Redis RPO ≤ 1 h nominal / zero task-loss via ledger; RTO ≤ 15 min. **Full-platform disaster (cluster loss): RTO ≤ 4 h, RPO ≤ 5 min** for system-of-record data.

Restore order (full DR): (1) provision cluster from `infra/` (Terraform + kustomize); (2) restore secrets from KMS; (3) restore Postgres via PITR to latest WAL; (4) start Redis empty, run ledger re-enqueue; (5) restore Qdrant snapshots; (6) deploy last-known-good digests from the release manifest; (7) run smoke suite; (8) flip DNS. The detailed step-by-step lives in RUNBOOK-04 (Section 12).

> **TITAN-OPS-018 (MUST)** — Backups MUST run per the Section 8.2 schedule, be encrypted, and be **verified by automated restore**: nightly CI restores the latest Postgres base+WAL into a scratch database and runs integrity checks.
> *Acceptance:* Nightly `backup-verify` job green; a BackupFailed page fires if the job is red or a scheduled backup is missing.

> **TITAN-OPS-019 (MUST)** — A full DR drill (cluster-loss simulation into a fresh staging cluster) MUST be executed quarterly and MUST meet RTO ≤ 4 h / RPO ≤ 5 min.
> *Acceptance:* Quarterly drill report in `ops/dr-drills/` records measured RTO/RPO; a miss opens a SEV-2 remediation issue.

---

## 9. Scaling Model & Backpressure

**What scales horizontally:** frontend, api, llm-gateway (per traffic); workers (per queue lag — the primary scaling dimension, since one "build me an app" request fans out into dozens of parallel agent tasks); Ollama pool (per GPU-node availability). **What scales vertically first:** PostgreSQL (then read replicas for read-heavy dashboard queries), Redis (then functional sharding: separate queue vs. cache instances), Qdrant (then sharded collections).

**Backpressure chain (must degrade gracefully, never collapse):**

```
 client → API admission → Postgres ledger → Redis Streams → worker pool → LLM gateway
            │                                   │                            │
            │ 429 + Retry-After when            │ XADD MAXLEN ~1M cap;       │ per-provider token-
            │ project concurrent-task           │ KEDA scales workers on     │ bucket rate limits;
            │ quota exceeded                    │ XPENDING lag; alert at     │ bounded retries w/
            │                                   │ oldest-pending > 300 s     │ jitter; circuit
            │                                   │                            │ breaker → fallback
            └── intake keeps accepting & ledgering (durable) even when execution is saturated;
                tasks wait in "queued" state visible in the dashboard with honest ETAs
```

Rules: admission control happens at the API (per-project concurrency quotas, default 25 concurrent tasks); the ledger accepts writes even under execution saturation so no work is refused-and-lost; workers pull (never pushed), so a slow worker only slows itself; the gateway enforces per-provider rate limits with a circuit breaker that trips to the fallback chain (Section 10). Scale-out beyond ~5,000 tasks/min on Redis Streams triggers the documented NATS JetStream migration path (ADR-OPS-4).

> **TITAN-OPS-020 (MUST)** — The platform MUST remain available for intake (accept + ledger + honest status) even when the execution tier is saturated; saturation manifests as queue wait, never as data loss or cascading failure.
> *Acceptance:* k6 test at 5× worker capacity for 30 min: zero 5xx on intake endpoints, zero lost tasks, `QueueBackpressure` alert fires, and the system fully drains after load stops.

---

## 10. Cost Optimization & LLM Spend Controls

LLM tokens are TITAN's dominant marginal cost; controls are enforced **in the gateway** and configured per org/project in PostgreSQL.

**Budget hierarchy:** global monthly ceiling → per-organization budget → per-project budget → per-task cap (default $2.00, configurable). The gateway checks remaining budget pre-call (prompt tokens × price table) and settles post-call with actual usage. At 80% of a project budget: in-app + ticket notification. At 100%: hard stop — new LLM calls are rejected, tasks pause in a resumable `budget_hold` state, and the owner is prompted to raise the budget or switch to local models.

**Routing & fallback ladder (configuration, not code — per the multi-LLM gateway contract in Volume 7):**

1. **Tiered model routing:** each agent role declares a capability tier; the gateway maps tiers to models via config. Planning/architecture roles get frontier models; mechanical tasks (formatting, summarizing progress reports, doc stubs) route to small/cheap or local models. Default config ships with a cost-optimized mapping.
2. **Local-model fallback:** on provider outage, rate-limit exhaustion, or budget hold, the gateway falls back per its chain, e.g. `anthropic → openai → ollama`. Falling back to Ollama is always legal (free tier is a platform guarantee); results are tagged `degraded_model=true` so the Code Reviewer Agent applies stricter review (Volume 6).
3. **Caching:** gateway-level exact + normalized prompt cache in Redis (TTL 24 h, keyed on model+prompt hash, opt-out per call) — deduplicates repeated agent boilerplate; provider-native prompt caching enabled where supported.
4. **Context discipline:** Volume 5's context-compression pipeline is the biggest cost lever; the gateway enforces per-role max-context budgets as a backstop and emits `titan_llm_context_truncated_total` when it clips.

**Infra cost controls:** worker scale-to-min off-hours (KEDA min replicas schedule); staging scale-to-zero nights/weekends (except nightly-job windows); GPU node pool for Ollama is optional and tainted so nothing else lands there; Loki/Tempo retention tiers to object storage. A monthly cost report (infra + LLM by project) is generated by a CronJob into the dashboard's admin area.

> **TITAN-OPS-021 (MUST)** — Per-project LLM budgets MUST be enforced pre-call at the gateway with hard stop at 100% and notification at 80%; paused tasks MUST resume without loss when budget is restored.
> *Acceptance:* Integration test: set a $0.50 project budget, run a task exceeding it → task enters `budget_hold`, no provider call exceeds the ceiling, raising the budget resumes and completes the task.

> **TITAN-OPS-022 (MUST)** — The gateway MUST support automatic fallback to local models (Ollama) on provider failure or budget exhaustion, controlled purely by configuration.
> *Acceptance:* With cloud provider keys revoked in staging, a submitted project still completes end-to-end on Ollama, with `degraded_model=true` recorded on affected tasks.

> **TITAN-OPS-023 (SHOULD)** — Tiered model routing per agent role SHOULD reduce blended cost ≥ 40% versus routing everything to the frontier model, measured on the standard benchmark project set (Volume 10 owns the benchmark).
> *Acceptance:* Benchmark report comparing single-model vs. tiered routing shows ≥ 40% cost reduction at equal-or-better QA pass rate.

---

## 11. Production Readiness Checklist

Release captains complete this checklist (tracked as a GitHub issue template, `ops/templates/prod-readiness.md`) before the **first** production launch and re-verify the delta items each release:

**Build & supply chain:** [ ] All images digest-pinned, cosign-signed, SBOM published; [ ] CVE scan clean or waivered per Volume 8; [ ] reproducible build verified for release tag.
**Deploy:** [ ] Blue/green flip and rollback exercised in staging this cycle; [ ] migration follows expand/contract and passed the previous-version compatibility job; [ ] feature flags for all new risky paths default OFF, owners assigned.
**Resilience:** [ ] Probes defined and validated (kill-pod drill); [ ] PDBs, anti-affinity, HPA/KEDA configured and load-tested; [ ] backpressure test (TITAN-OPS-020) green.
**Data:** [ ] Backups running and restore-verified (TITAN-OPS-018); [ ] PITR tested within 90 days; [ ] task-ledger reconciliation job green.
**Observability:** [ ] Five dashboards live; [ ] all Section 7 alerts loaded and test-fired; [ ] runbook links resolve; [ ] trace continuity verified (TITAN-OPS-016).
**Security (verify with Volume 8):** [ ] NetworkPolicies default-deny verified; [ ] secrets from external secret store, none in env-committed files; [ ] TLS everywhere; [ ] RBAC audit complete.
**Cost:** [ ] Budgets configured for every active org/project; [ ] spend-anomaly alert live; [ ] Ollama fallback verified (TITAN-OPS-022).
**Process:** [ ] On-call rotation staffed (min 2 people or documented solo-operator mode for self-hosters); [ ] SEV definitions and escalation path published; [ ] DR drill within last quarter.

> **TITAN-OPS-024 (MUST)** — No first production launch without 100% checklist completion signed by two maintainers; per-release delta items MUST be re-verified each release.
> *Acceptance:* The release issue shows every box checked with links to evidence (CI runs, drill reports) and two maintainer approvals.

---

## 12. Operational Runbooks (Outline)

Runbooks live in `ops/runbooks/`, one file each, linked from alerts, all following one skeleton: *Preconditions → Steps (copy-pasteable commands) → Verification → Escalation → Post-actions*.

**RUNBOOK-01 — Deploy (`deploy.md`):** preconditions (gates of §3.4 green, checklist delta done); steps: trigger `release.yml` from tag → watch green rollout → confirm smoke pass on preview URL → approve flip → watch SLO burn panel 30 min → confirm old-color scale-down; verification: deploy annotation on Platform Overview, error rate flat; escalation: any fast-burn → RUNBOOK-02; post: update release notes, close release issue.

**RUNBOOK-02 — Rollback (`rollback.md`):** decision matrix (auto vs. manual, warm-color vs. redeploy path); steps: `ops/scripts/rollback.sh all` → verify selector flip → verify SLO recovery → re-enable paused flags only after cause known; verification: error budget burn returns < 1×; escalation: rollback ineffective → declare incident (RUNBOOK-03), consider `agents.pause_all`; post: mandatory blameless post-mortem within 48 h, previous digest quarantined from re-promotion.

**RUNBOOK-03 — Incident (`incident.md`):** SEV definitions (SEV-1: platform down or data loss; SEV-2: SLO breach or major feature down; SEV-3: degraded); roles (commander, comms, operator); first 10 minutes: acknowledge page → open incident channel + status page → check Platform Overview and recent deploys (deploy? → RUNBOOK-02; provider outage? → verify gateway fallback; backpressure? → check KEDA scaling and quotas); kill-switch inventory (`agents.pause_all`, `intake.read_only`, `llm.provider_override`); status updates every 30 min for SEV-1; post: timeline from Loki/Tempo, post-mortem with action items as issues.

**RUNBOOK-04 — Backup & Restore (`backup-restore.md`):** verify-backup steps (check nightly job, list object-store artifacts); single-table recovery (PITR clone → dump table → surgical restore); full Postgres PITR (stop writers via `intake.read_only` → restore base → replay WAL to target time → validate ledger consistency → reopen); Qdrant snapshot restore + re-embedding fallback; Redis rebuild via ledger re-enqueue; full-DR sequence (§8.3) with expected timings per step; verification: smoke suite + reconciliation job; post: drill report or incident annex.

> **TITAN-OPS-025 (MUST)** — The four runbooks MUST exist as executable documents (tested command blocks), be linked from every paging alert, and be re-validated by drill at least quarterly.
> *Acceptance:* Each runbook has a `last-drilled` date within 90 days; alert annotations resolve to the correct runbook anchor.

---

## 13. Decision Records

**ADR-OPS-1 — Blue/green over rolling and canary for production releases.**
*Context:* TITAN needs sub-minute rollback and safe schema evolution with long-lived WebSockets and queue consumers. *Decision:* Blue/green with Service-selector flip, 30-min observation, shared datastores under expand/contract migrations. *Alternatives:* Rolling updates (simplest, but rollback is a full re-rollout with long mixed-version windows); canary via service mesh (finer risk control, but Istio/Linkerd is unjustified operational weight and hostile to the local-first goal); Argo Rollouts (deferred — see Open Questions). *Trade-offs:* accepted 2× capacity during deploys (idle color scales to zero between releases) and stricter migration discipline, in exchange for atomic flips and < 5-min rollback.

**ADR-OPS-2 — GitHub Actions as the sole CI/CD system.**
*Context:* Open-source monorepo on GitHub; contributors need free CI; TITAN's own DevOps Agent must be able to read and modify pipelines programmatically. *Decision:* GitHub Actions with OIDC-federated deploys, environments with required reviewers. *Alternatives:* GitLab CI (would force repo migration); Jenkins (self-hosted burden, plugin security surface); Argo CD pull-based GitOps (strong candidate, adds a controller to operate — revisit post-MVP; the manifests-in-git structure is already GitOps-compatible). *Trade-offs:* vendor coupling to GitHub accepted; mitigated by keeping all logic in repo-local scripts (`ops/scripts/`) callable from any runner, so workflows are thin wrappers.

**ADR-OPS-3 — Self-hosted kube-prometheus-stack + Loki over managed observability.**
*Context:* Local-first parity and zero-cost self-hosting are hard requirements; agent metrics (tokens, cost) are custom anyway. *Decision:* Prometheus + Grafana + Loki + otel-collector everywhere, provisioned as code, identical configs across environments. *Alternatives:* Datadog/New Relic (excellent UX; violates free/local requirement and per-host pricing punishes worker autoscaling); Grafana Cloud free tier (acceptable *optional* remote-write target, documented, not required). *Trade-offs:* we own storage sizing and Prometheus operations; retention beyond 30 d goes to object storage rather than a vendor.

**ADR-OPS-4 — Redis Streams at MVP with a documented NATS JetStream escape hatch.**
*Context:* Canonical Volume 1 decision; operations must define when and how it changes. *Decision:* Operate Redis Streams (with Sentinel, `noeviction`, ledger-backed durability) until any of: sustained > 5,000 tasks/min, consumer-group count > 200, or cross-region delivery is required; then execute the migration plan in Volume 3 (queue interface is abstracted, so workers swap consumers without task-model changes). *Alternatives:* NATS from day one (better semantics at scale, but one more stateful system for a laptop install); Kafka (operational overkill). *Trade-offs:* accepted at-least-once semantics with idempotent task handlers (dedup key = task ID) as the universal contract so the swap stays invisible.

**ADR-OPS-5 — Self-hosted Postgres-backed feature flags over a flag SaaS.**
*Context:* Flags gate releases, agent behavior, and kill switches; they must work offline on a laptop. *Decision:* `feature_flags` table + Redis cache + pub/sub invalidation, admin UI in the dashboard. *Alternatives:* LaunchDarkly (best-in-class, paid, network-dependent); Unleash self-hosted (viable, adds a service + DB; our needs are a strict subset). *Trade-offs:* no advanced experimentation analytics at MVP; percentage rollouts are deterministic hashes on org/project ID, which is sufficient for §5.3's flag classes.

---

## 14. Risks & Open Questions

**Risks**

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | LLM provider regional outage stalls all agent work | Medium | High | Gateway fallback ladder incl. Ollama (TITAN-OPS-022); degraded-model tagging; SLO-GW measured on fallback-inclusive success |
| R2 | Runaway agent loop burns budget rapidly | Medium | High | Per-task cost caps, per-project budgets, GlobalSpendAnomaly page, `agents.pause_all` kill switch |
| R3 | Blue/green schema discipline erodes (someone ships a breaking migration) | Medium | High | CI previous-version compatibility job (TITAN-OPS-013) is a hard gate, not a convention |
| R4 | Metric cardinality explosion from per-project labels | Medium | Medium | Label allowlist in otel-collector; recording rules; cap active-project label values; alert on series count |
| R5 | Self-hosters skip backups/DR entirely | High | High (for them) | Compose ships backup sidecar defaults ON; readiness checklist has a documented solo-operator mode; docs make restore a 1-script action |
| R6 | Single-cluster production is a zonal/regional single point of failure | Low | High | Multi-zone spread at MVP; documented DR rebuild ≤ 4 h; multi-region is explicitly out of scope until post-MVP (Open Question Q2) |

**Open Questions**

- **Q1:** Adopt Argo Rollouts to mechanize the blue/green flip and burn-rate analysis (replacing our release-job scripting), at the cost of another controller? Decide by end of MVP hardening; the Service-selector design is compatible either way.
- **Q2:** Multi-region active/passive for managed-cloud TITAN: at what user threshold does RTO ≤ 4 h become unacceptable? Requires pricing data from Volume 10's enterprise track.
- **Q3:** Per-agent-role K8s workload separation (dedicated Deployments per role for isolation/priority) vs. the current homogeneous worker pool with role dispatch — revisit when role-specific resource profiles diverge (e.g., browser-automation agents needing more memory).
- **Q4:** Should the provider price table (used for cost metrics) be fetched from a maintained community source or vendored and reviewed per release? Currently vendored in `config/llm-prices.yaml`; staleness risk is accepted and surfaced in the monthly cost report.
- **Q5:** GPU scheduling for the Ollama pool: time-slicing vs. MIG vs. whole-GPU nodes — depends on the model mix chosen in Volume 5's local-model policy.

---

## 15. Cross-References

- **Volume 1 — Foundation:** owns and justifies all canonical stack decisions applied here (Kubernetes, Postgres 16, Redis 7, Redis Streams, observability stack); NFR targets that Section 7 SLOs operationalize.
- **Volume 2 — Multi-Agent Intelligence:** the DevOps Agent's role spec — it executes the pipelines and runbooks defined in this volume; agent KPIs feed the Agent Operations dashboard.
- **Volume 3 — Backend Engineering:** service internals, the queue/message envelope (including the `traceparent` field), task ledger schema, WebSocket eventing, and the NATS migration plan referenced by ADR-OPS-4.
- **Volume 4 — Frontend & Dashboard:** the Deployment Dashboard, Monitoring views, budget notifications, and flag admin UI surfaced from this volume's data; WebSocket reconnect contract used during blue/green flips.
- **Volume 5 — Memory, Knowledge & RAG:** Qdrant recovery-by-re-embedding, context compression as the primary LLM cost lever, storage of prompt/completion bodies outside logs and traces.
- **Volume 6 — Engineering Workflow:** how deploy/rollback events feed back into agent task states; stricter review policy for `degraded_model=true` outputs.
- **Volume 7 — Tool Ecosystem & Integrations:** the LLM gateway's provider abstraction and configuration contract that Sections 6.1 and 10 instrument and govern.
- **Volume 8 — Security:** secrets management, KMS, NetworkPolicy details, image-signing policy enforcement, log redaction lists, RBAC for cluster access, CVE waiver process.
- **Volume 10 — Autonomous Evolution:** benchmark suite used by TITAN-OPS-023; agent performance metrics (review rejections, throughput) consumed by the self-improvement loop.

---

*End of Volume 9 — TITAN-VOL-09, Draft v1.0.*
