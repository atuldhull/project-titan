# PROJECT TITAN — root Makefile (Vol 3 §13.2: make dev / test / lint / migrate / seed)
# CI (.github/workflows/ci.yml) calls the SAME granular targets developers run
# locally — never a parallel command set. Requires GNU Make; on Windows run from
# Git Bash with make installed (e.g. `winget install ezwinports.make`) or WSL.

API_DIR := apps/api
WEB_DIR := apps/web
SCHEMAS_DIR := packages/schemas

.PHONY: dev test test-web test-schema test-llm-live lint lint-style boundaries route-guards \
        agent-purity typecheck migrate seed generate-types check-types-drift \
        lint-openapi secret-scan eval-retrieval eval-baseline bench-memory \
        budget-web

## dev: run the API (uvicorn, reload) and the web app (next dev) together.
## Docker Compose is the primary dev loop from Phase 2 onward.
dev:
	( cd $(API_DIR) && uv run --env-file $(CURDIR)/.env uvicorn titan.main:app --reload --port 8000 ) & \
	( cd $(WEB_DIR) && npm run dev ) & \
	wait

test:
	cd $(API_DIR) && uv run pytest -q

# Phase 57: the frontend data layer (vitest — BFF route handlers, the WS state
# machine incl. seq-gap/reconnect, RBAC gating, toast policy). jsdom, no browser.
test-web:
	cd $(WEB_DIR) && npm test

# TITAN-FRT-005: /org First Load JS <= 300 kB gzipped. Requires a prior
# `next build` (the CI job runs both).
budget-web:
	cd $(WEB_DIR) && npm run build
	cd $(WEB_DIR) && npm run budget:org

# Style + formatting gates (ruff incl. ASYNC + ADR-007 banned imports, eslint, prettier)
lint-style:
	cd $(API_DIR) && uv run ruff check .
	cd $(API_DIR) && uv run ruff format --check .
	cd $(WEB_DIR) && npm run lint
	cd $(WEB_DIR) && npm run format:check
	cd $(WEB_DIR) && npm run lint:proofs

# Module boundaries: TITAN-BCK-001/004 independence + ADR-007 provider-SDK contract
boundaries:
	cd $(API_DIR) && uv run lint-imports

# TITAN-BCK-021 / TITAN-SEC-030: every API route must carry an RBAC guard.
route-guards:
	cd $(API_DIR) && uv run python scripts/check_route_guards.py

# TITAN-INT-001: agent code may not spawn processes, write files, or open
# sockets/HTTP clients directly — every side-effect goes through the Tool Bus.
agent-purity:
	cd $(API_DIR) && uv run python scripts/check_agent_purity.py

# Static typing: mypy --strict (api) + tsc --noEmit strict (web, schemas)
typecheck:
	cd $(API_DIR) && uv run mypy
	cd $(WEB_DIR) && npm run typecheck
	cd $(SCHEMAS_DIR) && npm run typecheck

# Developer catch-all — the union of every static gate CI runs.
lint: lint-style boundaries route-guards agent-purity typecheck lint-openapi

# TITAN-BCK-027: OpenAPI -> TypeScript bridge into packages/schemas.
# TITAN-INT-010: the plugin-manifest JSON Schema is published alongside it so third
# parties can validate titan-plugin.yaml before install; check-types-drift covers it.
generate-types:
	cd $(API_DIR) && uv run python scripts/export_openapi.py ../../$(SCHEMAS_DIR)/openapi.json
	cd $(API_DIR) && uv run python scripts/export_plugin_schema.py ../../$(SCHEMAS_DIR)/plugin-manifest.v1.json
	cd $(SCHEMAS_DIR) && npm run generate

# CI drift gate: regenerating must produce zero diff against the committed
# types — including brand-new untracked generated files.
check-types-drift: generate-types
	git diff --exit-code -- $(SCHEMAS_DIR)
	@test -z "$$(git status --porcelain -- $(SCHEMAS_DIR) | grep '^??')" || { git status --porcelain -- $(SCHEMAS_DIR) | grep '^??'; echo 'untracked generated files in $(SCHEMAS_DIR)'; exit 1; }

# TITAN-BCK-006 acceptance: spectral ruleset over the OpenAPI document.
lint-openapi:
	cd $(SCHEMAS_DIR) && npm run lint:openapi

# TITAN-FND-062: secret scanning — same engine and mode CI uses (git history),
# via Docker locally. MSYS2_ARG_CONV_EXCL stops Git Bash mangling /repo.
secret-scan:
	MSYS2_ARG_CONV_EXCL='*' docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:latest detect --source /repo --no-banner --config /repo/.gitleaks.toml

# --env-file pins the repo-root .env so host-side runs always target THIS
# project's containers (ports may be remapped there), never defaults.
migrate:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env alembic upgrade head

seed:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env python scripts/seed.py

# Schema/cycle tests against the disposable test database (never the dev DB).
test-schema:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env pytest -m schema --no-cov

# Live-provider LLM conformance (TITAN-INT-060): hits real Anthropic/OpenAI/
# Google/Ollama when keys are present, skipping each leg otherwise. Runs as a
# separate, NON-BLOCKING CI job so real API divergence is caught without gating
# the main pipeline (no coverage gate here).
test-llm-live:
	cd $(API_DIR) && uv run pytest -m live_llm --no-cov

# Local runs read TITAN_TEST_DATABASE_URL from .env; CI runners have no .env
# (gitignored) and inject it as a job env var instead — and `uv run
# --env-file <missing>` HARD-FAILS, so the flag must be conditional.
ENV_FILE_FLAG := $(if $(wildcard $(CURDIR)/.env),--env-file $(CURDIR)/.env,)

# TITAN-BRN-011: the retrieval quality gate. Seeds the versioned golden
# dataset, runs recall@10 + nDCG@10 in all three reranker modes, fails on a
# >5% regression vs tests/golden/memory_eval_baseline.json — plus the
# broken-fusion-weight negative control and the TITAN-BRN-004 rebuild gate.
eval-retrieval:
	cd $(API_DIR) && uv run $(ENV_FILE_FLAG) pytest tests/test_memory_eval_gate.py -q -s --no-cov

# Consciously refresh the stored eval baseline (dataset bump / real retrieval
# improvement) — commit the resulting memory_eval_baseline.json diff.
eval-baseline:
	cd $(API_DIR) && TITAN_EVAL_WRITE_BASELINE=1 uv run $(ENV_FILE_FLAG) pytest \
		"tests/test_memory_eval_gate.py::test_eval_gate_against_baseline" -q -s --no-cov

# TITAN-BRN-010: the 100k-chunk query->packet latency benchmark (reference
# laptop, real PG/Redis/Qdrant; see scripts/bench_retrieval.py for setup).
bench-memory:
	cd $(API_DIR) && uv run python scripts/bench_retrieval.py $(BENCH_ARGS)

# Phase 55 (TITAN-FND-007/052): the chaos suite + the Section-7 KPI benchmark over
# the fixed 10-brief suite. NIGHTLY ONLY (nightly.yml), never per-commit — the
# chaos scenarios SIMULATE killing each service (in-process) and the runner executes
# the one executable brief end-to-end while validating the other nine as fixtures.
# Uses an in-process queue (fakeredis) so no shared Redis is touched; the disposable
# test DB (TITAN_TEST_DATABASE_URL) receives the benchmark + chaos rows.
benchmark-suite:
	cd $(API_DIR) && uv run $(ENV_FILE_FLAG) pytest --no-cov \
		tests/test_chaos_reconcile.py tests/test_chaos_suite.py \
		tests/test_benchmark_suite.py tests/test_benchmark_runner.py \
		tests/test_benchmark_cli.py
