# PROJECT TITAN — root Makefile (Vol 3 §13.2: make dev / test / lint / migrate / seed)
# CI (.github/workflows/ci.yml) calls the SAME granular targets developers run
# locally — never a parallel command set. Requires GNU Make; on Windows run from
# Git Bash with make installed (e.g. `winget install ezwinports.make`) or WSL.

API_DIR := apps/api
WEB_DIR := apps/web
SCHEMAS_DIR := packages/schemas

.PHONY: dev test test-schema lint lint-style boundaries typecheck migrate seed \
        generate-types check-types-drift secret-scan

## dev: run the API (uvicorn, reload) and the web app (next dev) together.
## Docker Compose is the primary dev loop from Phase 2 onward.
dev:
	( cd $(API_DIR) && uv run --env-file $(CURDIR)/.env uvicorn titan.main:app --reload --port 8000 ) & \
	( cd $(WEB_DIR) && npm run dev ) & \
	wait

test:
	cd $(API_DIR) && uv run pytest -q

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

# Static typing: mypy --strict (api) + tsc --noEmit strict (web, schemas)
typecheck:
	cd $(API_DIR) && uv run mypy
	cd $(WEB_DIR) && npm run typecheck
	cd $(SCHEMAS_DIR) && npm run typecheck

# Developer catch-all — the union of every static gate CI runs.
lint: lint-style boundaries typecheck lint-openapi

# TITAN-BCK-027: OpenAPI -> TypeScript bridge into packages/schemas
generate-types:
	cd $(API_DIR) && uv run python scripts/export_openapi.py ../../$(SCHEMAS_DIR)/openapi.json
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
