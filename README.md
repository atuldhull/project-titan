# PROJECT TITAN

A platform where a user types a product brief ("Build me a Netflix clone") and a
team of specialized AI agents — organized like a real software company — plans,
designs, builds, tests, reviews, secures, documents, and deploys the application.
The human acts as the client.

The guiding principle is that **the model is not trusted**. Authority, budgets,
tool permissions, and secret redaction are enforced in code, outside the LLM, so
a jailbroken or confused agent still cannot exceed its mandate. The red-team
corpus targets those hard controls rather than the model's (inevitably
bypassable) soft behaviour.

**The complete specification lives in [`Project-TITAN-Docs/`](Project-TITAN-Docs/)
and is the single source of truth for every decision.** The build was executed
phase by phase per
[`Project-TITAN-Docs/TITAN-BUILD-PHASES.md`](Project-TITAN-Docs/TITAN-BUILD-PHASES.md).

**Security:** to report a vulnerability, see [`SECURITY.md`](SECURITY.md)
(private reporting, response SLAs, 90-day coordinated disclosure). Control
traceability is machine-readable in
[`security/compliance-map.yaml`](security/compliance-map.yaml) (TITAN-SEC-110).

---

## Status

**All 86 planned phases are built** — milestones M1 through M8, including M8,
which the plan itself marks optional/post-MVP. The final phase was *Phase 86 —
TITAN-Bench, A/B harness, auto-rollback & debate protocol*.

| | |
|---|---|
| Backend | 144k lines of Python, 40 bounded modules, 42 migrations |
| Frontend | 60k lines of TypeScript/React, 26 routes |
| Tests | **2,549 passing** of 2,581 backend tests, plus 65 frontend test files |
| Types | `mypy --strict` clean across 663 source files; `tsc --noEmit` clean |
| Infra | 20-service Compose stack, 8 Dockerfiles, k8s base, Terraform modules |

### What is verified

The backend suite runs in CI against real Postgres and reports **2,549 passed,
26 failed, 6 skipped**. The dashboard boots and is driven end-to-end by
Playwright (`e2e-web` passes), and the retrieval evaluation, bundle budget,
Storybook coverage, and live-LLM gate all pass.

### Known gaps

Honest accounting of what is still red:

- **26 backend test failures**, of which **21 are a CI environment gap**, not
  product defects: the `test` job does not build the `titan-sandbox:local` and
  `titan-browser:local` images, so every test exercising the sandboxed tool
  layer (code execution, terminal, git, docker, deploy, browser) fails at image
  lookup rather than on logic. One `IntegrityError` in
  `test_evolution_scorecard` is still under investigation.
- **`secrets`** — gitleaks reports 22 findings, all of them deliberate fake
  fixtures in the redaction tests and the red-team corpus. No real credential is
  present; `.env` has never been tracked, and the custom vault-identity rule
  fires zero times. The allowlist in [`.gitleaks.toml`](.gitleaks.toml) covers
  three Phase-47 fixtures and was never extended to the Phase-69/75 ones.
- **`dependency-audit`** — three routine CVE bumps (pyjwt, urllib3, soupsieve).
- **`k8s-manifests`** — the kustomize installer aborts because the GitHub runner
  image now preinstalls it.
- **`sast`** — one semgrep hit on a built `UPDATE` statement in
  `loop/repository.py`. Reviewed: the `SET` clause is assembled only from
  hardcoded literals and all caller data is passed as bound parameters, so it is
  a false positive.
- **`types-drift`**, **`web-visual`** — not yet triaged.

An end-to-end run of a complete agent-driven product build has **not** yet been
demonstrated. That is the next milestone, and it depends on the sandbox images
above.

---

## Repository layout (ADR-001, Vol 3 §13.2)

```
titan/
├── apps/
│   ├── web/          # Next.js 15 + TypeScript + Tailwind + shadcn/ui dashboard
│   └── api/          # Python 3.12 + FastAPI modular monolith (40 bounded modules)
├── packages/
│   ├── schemas/      # TypeScript types generated from the OpenAPI spec
│   ├── api-client/   # typed REST client over openapi-fetch
│   └── ws-protocol/  # shared websocket message contracts
├── config/           # agent definitions (YAML) + model policy profiles
├── plugins/          # first-party connectors: github, gitlab, jira, linear, figma, …
├── evolution/        # prompt registry, scoring, bench, debate, proposals
├── infra/            # Docker, Keycloak, observability, k8s, Terraform, backup
├── ops/              # runbooks, DR drills, operational scripts
├── security/         # compliance map, policies
├── docs/             # repo-level docs; specification series is Project-TITAN-Docs/
└── Makefile          # make dev / test / lint / migrate / seed
```

Note: Vol 3 §13.2 names the shared-types package `packages/shared-types`; ADR-001
and the phase plan name it `packages/schemas`. This repo follows ADR-001
(`packages/schemas`).

---

## The agent organization

Sixteen roles are defined declaratively in [`config/agents/`](config/agents/).
Each YAML sets its tier, supervisor, prompt template, memory read/write scopes,
and an `authority` block splitting what the agent **decides alone** from what it
**must escalate**, and to whom.

| Tier | Agents |
|---|---|
| Executive | `ceo` |
| Planning | `orchestrator`, `pm_product`, `pm_project`, `architect` |
| Engineering | `eng_backend`, `eng_frontend`, `eng_database`, `eng_devops` |
| Quality & Safety | `qa`, `reviewer`, `critic`, `security` |
| Support | `research`, `docs`, `memory` |

For example, the Orchestrator decides task ordering, parallel-vs-serial
execution, and retries on its own — but must escalate milestone changes and
token-budget exhaustion to the CEO agent, and architectural sequencing disputes
to the Architect.

---

## Prerequisites

- **uv** (manages Python 3.12 and all backend dependencies)
- **Node.js ≥ 20** and npm
- **GNU Make** (Git Bash: `winget install ezwinports.make`; or use WSL)
- **Docker Desktop** — needed for the Compose stack and the sandboxed tool layer
- **An LLM** — either [Ollama](https://ollama.com) locally (free) or a cloud API key

## Running it

See **[`RUN.md`](RUN.md)** for the full quickstart. In short:

```bash
cp .env.example .env
docker compose up -d postgres redis qdrant   # datastores
docker compose run --rm migrate               # alembic upgrade head
docker compose up -d --build api worker       # API :8000 + the agent worker
```

Agents run on **either** local Ollama (free, slower — `qwen2.5-coder:7b` plus
`nomic-embed-text`) **or** a cloud LLM key. For a fully offline, $0 run:

```bash
TITAN_MODELS_CONFIG_PATH=config/models.local.yaml
TITAN_MODEL_POLICY_PROFILE=local-free
TITAN_OLLAMA_BASE_URL=http://host.docker.internal:11434
```

On Windows, run the API and worker **inside** Compose. `RUN.md` explains why: a
Docker Desktop loopback bug otherwise drops host↔container DB connections
(`WinError 64`) and stalls the agents.

---

## Commands

| Command | What it does |
|---|---|
| `make lint` | ruff + eslint + prettier across api and web |
| `make typecheck` | `mypy --strict` (api) + `tsc --noEmit` (web) |
| `make test` | pytest for the backend |
| `make test-web` / `make e2e-web` | Vitest unit tests / Playwright end-to-end |
| `make dev` | uvicorn on :8000 and next dev on :3000 (pre-Compose dev loop) |
| `make migrate` | alembic upgrade head against this repo's database |
| `make seed` | seed §9.3 roles + local admin (idempotent) |
| `make security-check` | secret scan, semgrep, dependency audit, SBOM |
| `make redteam-run` | the LLM red-team harness |
| `make dr-drill` | disaster-recovery rehearsal with an RTO/RPO gate |

There are many more targets — module boundaries, route guards, compliance
checks, benchmarks, and evaluation suites.

Backend module boundaries are enforced by import-linter (TITAN-BCK-001/004):
domain modules may only be imported through their public `service.py` interface.

---

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs 19 jobs in a fixed
stage order (Vol 9 §3.1): **lint → typecheck → tests**, with the security,
supply-chain, and infrastructure gates running in parallel. Nightly
([`nightly.yml`](.github/workflows/nightly.yml)) adds benchmarks, container
image rescanning, and backup verification. Every CI job calls the same Makefile
target a developer runs locally.

---

## Documentation

[`Project-TITAN-Docs/`](Project-TITAN-Docs/) holds ten specification volumes
plus the phase plan and build prompts:

1. Foundation & Product Requirements
2. Multi-Agent Intelligence
3. Backend Engineering & Infrastructure
4. Frontend & Dashboard
5. AI Brain — Memory, Knowledge, RAG
6. Engineering Workflow & Standards
7. Integrations & Tool Ecosystem
8. Security
9. Production & Operations
10. Future & Autonomous Evolution
