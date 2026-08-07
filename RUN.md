# Running TITAN locally

A working quickstart for a fresh machine. TITAN is a FastAPI backend (`apps/api`) + a
Next.js dashboard (`apps/web`) + Postgres, Redis, and Qdrant, driven by a background
worker. Agents run on either **local Ollama (free)** or a **cloud LLM key**.

## Prerequisites
- **Docker Desktop** (running)
- **uv** (Python 3.12 toolchain) and **Node ≥ 20**
- GNU Make (optional; the commands below don't require it)
- **An LLM**: either [Ollama](https://ollama.com) with the models pulled (free, slower):
  ```bash
  ollama pull qwen2.5-coder:7b
  ollama pull nomic-embed-text
  ```
  …or an `ANTHROPIC_API_KEY` (fast, paid).

## 1. Configure `.env`
```bash
cp .env.example .env
```
The defaults target the Compose stack. For a **fully-offline, $0 run on Ollama**, add:
```bash
TITAN_MODELS_CONFIG_PATH=config/models.local.yaml
TITAN_MODEL_POLICY_PROFILE=local-free
# Containers reach the host's Ollama via host.docker.internal (NOT localhost):
TITAN_OLLAMA_BASE_URL=http://host.docker.internal:11434
```
For a **cloud run** instead, set `ANTHROPIC_API_KEY=…` and leave the model config default.

## 2. Recommended: run the backend in Compose (avoids a Windows/Docker host-networking bug)
Running the API + worker *inside* Compose makes them talk to Postgres/Redis/Qdrant over the
internal Docker network — which sidesteps a Docker Desktop loopback issue on Windows that
otherwise drops host↔container DB connections (`WinError 64`) and stalls the agents.

```bash
docker compose up -d postgres redis qdrant          # datastores
docker compose run --rm migrate                      # or: alembic upgrade head
docker compose up -d --build api worker              # API :8000 + the agent worker
```
Seed roles + a local admin (idempotent):
```bash
cd apps/api && uv run --env-file ../../.env python scripts/seed.py && cd ../..
```
Run the dashboard (host dev server — talks to the API on :8000):
```bash
cd apps/web && npm install && PORT=3002 npm run dev
```
Verify: `curl http://localhost:8000/readyz` → `{"status":"ready", postgres/redis/qdrant: ok}`.

## 3. Use it
- Open **http://localhost:3002** and log in as **`admin@titan.local` / `titan-local-admin`**
  (login is browser-only via OAuth/PKCE; the password grant is disabled by design).
- Create a project with a brief (e.g. *"Build a small todo web app"*). The agents plan →
  build → review. On local Ollama this is **slow (minutes per turn)** but free; a cloud key
  is much faster.

## Alternative: host dev loop (no Compose)
`make dev` runs uvicorn + next on the host. It works, but on Windows the Docker loopback
can drop DB connections under the worker's load — prefer the Compose path above for anything
beyond a quick API poke.

## Verifying the build
```bash
cd apps/api && uv run ruff check src tests && uv run mypy src && uv run pytest -q
cd ../web && npx tsc --noEmit && npx eslint .
```
Backend module boundaries are enforced by import-linter; migrations are hand-written and
gated by `scripts/check_migrations.py`.

## Notes / known constraints
- **Windows + Docker Desktop**: the host↔container loopback can degrade under sustained load
  (`WinError 64`). If the worker or a host-run test starts erroring on DB connections,
  `docker compose restart postgres` (or restart Docker Desktop) clears it. The Compose-internal
  network path is unaffected.
- **Data lives in Docker volumes**, not the repo — a fresh clone starts with an empty DB
  (`migrate` + `seed`). Ollama models are pulled separately (gigabytes, not in the repo).
- The full specification is in `Project-TITAN-Docs/`.
