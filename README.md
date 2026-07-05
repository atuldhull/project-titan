# PROJECT TITAN

A platform where a user types a product brief ("Build me a Netflix clone") and a
team of specialized AI agents — organized like a real software company — plans,
designs, builds, tests, reviews, secures, documents, and deploys the application.
The human acts as the client.

**The complete specification lives in [`Project-TITAN-Docs/`](Project-TITAN-Docs/)
and is the single source of truth for every decision.** The build is executed
phase by phase per
[`Project-TITAN-Docs/TITAN-BUILD-PHASES.md`](Project-TITAN-Docs/TITAN-BUILD-PHASES.md).

## Repository layout (ADR-001, Vol 3 §13.2)

```
titan/
├── apps/
│   ├── web/          # Next.js 15 + TypeScript + Tailwind + shadcn/ui dashboard
│   └── api/          # Python 3.12 + FastAPI modular monolith (12 bounded modules)
├── packages/
│   └── schemas/      # TypeScript types generated from the OpenAPI spec (Phase 3)
├── infra/            # Docker, Keycloak, observability, k8s (from Phase 2)
├── docs/             # repo-level docs; specification series is Project-TITAN-Docs/
└── Makefile          # make dev / test / lint / migrate / seed
```

Note: Vol 3 §13.2 names the shared-types package `packages/shared-types`; ADR-001
and the phase plan name it `packages/schemas`. This repo follows ADR-001
(`packages/schemas`).

## Prerequisites

- **uv** (manages Python 3.12 and all backend dependencies)
- **Node.js ≥ 20** and npm
- **GNU Make** (Git Bash: `winget install ezwinports.make`; or use WSL)
- Docker Desktop — needed from Phase 2 onward, not for Phase 1

## Commands

| Command | What it does |
|---|---|
| `make lint` | ruff + mypy --strict + import-linter (api), eslint + tsc --strict (web) |
| `make test` | pytest for the backend |
| `make dev` | uvicorn on :8000 and next dev on :3000 (pre-Compose dev loop) |
| `make migrate` | alembic upgrade head against this repo's database |
| `make seed` | seed §9.3 roles + local admin (idempotent) |

Backend module boundaries are enforced by import-linter (TITAN-BCK-001/004):
domain modules may only be imported through their public `service.py` interface.

## Build status

- [x] Phase 1 — Monorepo scaffold & toolchain
- [x] Phase 2 — Compose core stack + FastAPI skeleton + health
- [x] Phase 3 — CI quality gates + Pydantic→TypeScript bridge
- [x] Phase 4 — Observability foundation in Compose
- [x] Phase 5 — Dashboard shell (13-surface nav + design system) — M1 complete
- [x] Phase 6 — PostgreSQL schema, migrations & cycle check (starts M2)
- [x] Phase 7 — REST conventions framework + gateway middleware
- [x] Phase 8 — Identity: local auth, JWTs, API keys, service tokens
- [ ] Phase 9 — RBAC engine + route protection + ownership (next)
