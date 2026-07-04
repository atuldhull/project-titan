# PROJECT TITAN — root Makefile (Vol 3 §13.2: make dev / test / lint / migrate / seed)
# Requires GNU Make. On Windows, run from Git Bash with make installed
# (e.g. `winget install ezwinports.make`) or from WSL.

API_DIR := apps/api
WEB_DIR := apps/web

.PHONY: dev test lint migrate seed

## dev: run the API (uvicorn, reload) and the web app (next dev) together.
## Docker Compose replaces this as the primary dev loop in Phase 2.
dev:
	( cd $(API_DIR) && uv run --env-file $(CURDIR)/.env uvicorn titan.main:app --reload --port 8000 ) & \
	( cd $(WEB_DIR) && npm run dev ) & \
	wait

test:
	cd $(API_DIR) && uv run pytest -q

lint:
	cd $(API_DIR) && uv run ruff check .
	cd $(API_DIR) && uv run ruff format --check .
	cd $(API_DIR) && uv run mypy
	cd $(API_DIR) && uv run lint-imports
	cd $(WEB_DIR) && npm run lint
	cd $(WEB_DIR) && npm run format:check
	cd $(WEB_DIR) && npm run typecheck
	cd packages/schemas && npm run typecheck

# --env-file pins the repo-root .env so host-side runs always target THIS
# project's containers (ports may be remapped there), never defaults.
migrate:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env alembic upgrade head

seed:
	@echo "seed: not yet wired: the seed script arrives in Phase 6"
