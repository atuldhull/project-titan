# PROJECT TITAN — root Makefile (Vol 3 §13.2: make dev / test / lint / migrate / seed)
# CI (.github/workflows/ci.yml) calls the SAME granular targets developers run
# locally — never a parallel command set. Requires GNU Make; on Windows run from
# Git Bash with make installed (e.g. `winget install ezwinports.make`) or WSL.

API_DIR := apps/api
WEB_DIR := apps/web
SCHEMAS_DIR := packages/schemas
# Overridden in CI with the digest-pinned build tag; a sane default for local runs.
TITAN_IMAGE ?= titan-api:local

.PHONY: dev test test-web test-schema test-llm-live lint lint-style boundaries route-guards \
        agent-purity typecheck migrate seed generate-types check-types-drift \
        lint-openapi secret-scan eval-retrieval eval-baseline bench-memory \
        budget-web e2e-web stories-web visual-web visual-web-update check-stale-flags

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

# Phase 65: the responsive (TITAN-FRT-041) + accessibility (TITAN-FRT-042/043/
# 045/046) + palette & shortcut (TITAN-FRT-007/048) sweep, in a real browser.
#
# Hermetic: every API response is served by page.route fixtures typed against the
# generated OpenAPI contract, and the session is a locally minted cookie — so
# this needs Chromium but NOT Postgres, Qdrant or Ollama. The offline-against-
# Compose run TITAN-FRT-004 asks for is Phase 66's job, not this one.
# Playwright's own webServer runs `next build && next start` on :3100.
e2e-web:
	cd $(WEB_DIR) && npx playwright install --with-deps chromium
	cd $(WEB_DIR) && npm run e2e

# TITAN-FRT-002 clause 1: every one of the 13 §6 screens has a story for each of
# loading / empty / error / populated. Reads the BUILT Storybook index, so a
# story that fails to load counts as missing rather than as present.
stories-web:
	cd $(WEB_DIR) && npm run build-storybook
	cd $(WEB_DIR) && npm run stories:coverage

# TITAN-FRT-002 clause 2: the visual regression suite that snapshots all four
# states — and, just as importantly, the only thing that proves those 52 stories
# RENDER. Coverage reads an index; a story lands in it whether or not it throws.
#
# Runs inside the pinned Playwright image on BOTH sides of the comparison. The
# baselines in e2e-storybook/__snapshots__ are Linux, produced by this exact
# image, and the tolerance is zero — so a host-native run WILL differ on font
# rasterisation and is not how this suite is invoked. `visual-web-update` is how
# you re-record after an intended visual change; commit what it writes.
#
# The named volume shadows apps/web/node_modules so a Linux `npm ci` inside the
# container cannot overwrite a developer's host install with Linux binaries.
PLAYWRIGHT_IMAGE := mcr.microsoft.com/playwright:v1.61.1-noble
VISUAL_DOCKER := docker run --rm -v "$(CURDIR):/work" -v titan-web-nm:/work/$(WEB_DIR)/node_modules -w /work/$(WEB_DIR) $(PLAYWRIGHT_IMAGE)

visual-web:
	cd $(WEB_DIR) && npm run build-storybook
	$(VISUAL_DOCKER) bash -c "npm ci --no-audit --no-fund && npm run visual"

visual-web-update:
	cd $(WEB_DIR) && npm run build-storybook
	$(VISUAL_DOCKER) bash -c "npm ci --no-audit --no-fund && npm run visual:update"

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

# TITAN-SEC-071: semgrep bans string-built SQL across production code AND
# migrations. Docker, like gitleaks (semgrep has no first-class Windows binary),
# so the fast per-phase `lint` loop stays hermetic — this runs in CI + on demand.
# --error exits non-zero on any finding. tests/fixtures/ is excluded on purpose:
# it holds the deliberately-vulnerable positive case the rule self-test fires on.
# SEC-071 string-built SQL + SEC-073 shell-string exec + SEC-072 dynamic eval/exec.
# One docker run over all three rules; src/ + alembic/ are clean, tests/fixtures/ is
# scanned only by the self-test below (it holds the deliberately-vulnerable cases).
semgrep:
	MSYS2_ARG_CONV_EXCL='*' docker run --rm -v "$(CURDIR):/repo" semgrep/semgrep:latest \
		semgrep --config /repo/apps/api/.semgrep/sql-injection.yml \
		--config /repo/apps/api/.semgrep/no-shell-exec.yml \
		--config /repo/apps/api/.semgrep/no-dynamic-exec.yml \
		--error --quiet /repo/apps/api/src /repo/apps/api/alembic

# Proves each rule actually bites (Phase 65/66 vacuous-gate discipline): semgrep
# MUST find violations in every committed vulnerable fixture, else the rule is
# inert. Each config is run against ITS fixture; exit 0 only if all three fire.
semgrep-selftest:
	@for pair in \
		"sql-injection:sql_injection_sample:SEC-071" \
		"no-shell-exec:shell_exec_sample:SEC-073" \
		"no-dynamic-exec:dynamic_exec_sample:SEC-072"; do \
		rule=$${pair%%:*}; rest=$${pair#*:}; fix=$${rest%%:*}; sec=$${rest##*:}; \
		MSYS2_ARG_CONV_EXCL='*' docker run --rm -v "$(CURDIR):/repo" semgrep/semgrep:latest \
			semgrep --config /repo/apps/api/.semgrep/$$rule.yml \
			--error --quiet /repo/apps/api/tests/fixtures/$$fix.py; \
		test $$? -ne 0 || { echo "$$sec semgrep rule did NOT fire on its fixture ($$fix.py)"; exit 1; }; \
	done; echo "all 3 semgrep rules fire on their vulnerable fixtures"

# --- TITAN-SEC-100..105: supply-chain scanning (Vol 8 §11) -------------------
# Each tool runs in CI (its own job) and on demand here. Like semgrep/gitleaks
# they are Docker/CLI images so the fast per-phase `lint` loop stays hermetic.
# HIGH/CRITICAL findings block; a documented, expiring exception lives in
# security/scan-exceptions.yaml (SEC-100).

# SEC-100: Python SAST — bandit over src/ (not tests) at high severity/confidence.
# uvx runs it ephemerally so it never enters the project lockfile. [tool.bandit] in
# apps/api/pyproject.toml holds the config.
sast-python:
	cd $(API_DIR) && uvx 'bandit[toml]' -q -c pyproject.toml --severity-level high --confidence-level high -r src

# SEC-103: known-vulnerable Python deps from the FROZEN lockfile (not the loose
# manifest). Export the resolved runtime set to a requirements file, then audit it.
audit-python:
	cd $(API_DIR) && uv export --frozen --no-emit-project --no-dev -o "$(CURDIR)/titan-req.txt"
	uvx pip-audit --strict --desc -r "$(CURDIR)/titan-req.txt"
	rm -f "$(CURDIR)/titan-req.txt"

# SEC-103: known-vulnerable JS deps (web + client packages), high+ only.
audit-web:
	cd $(WEB_DIR) && npm audit --omit=dev --audit-level=high

# SEC-104/105: cross-ecosystem OSV scan of the committed lockfiles.
osv-scan:
	MSYS2_ARG_CONV_EXCL='*' docker run --rm -v "$(CURDIR):/repo" ghcr.io/google/osv-scanner:latest \
		scan --lockfile=/repo/apps/api/uv.lock --lockfile=/repo/apps/web/package-lock.json

# SEC-101: SBOM (CycloneDX) for the repo, emitted as a CI artifact.
sbom:
	MSYS2_ARG_CONV_EXCL='*' docker run --rm -v "$(CURDIR):/repo" anchore/syft:latest \
		scan dir:/repo -o cyclonedx-json=/repo/sbom.cdx.json

# SEC-101: container image vuln scan (built image), HIGH/CRITICAL block. No
# --ignore-unfixed (SEC-104 review #6): an unfixed HIGH/CRITICAL CVE is not a free
# pass — accepting one requires a time-boxed security/scan-exceptions.yaml entry,
# not a blanket flag that silently ships every unpatched vuln.
image-scan:
	MSYS2_ARG_CONV_EXCL='*' docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		aquasec/trivy:latest image --severity HIGH,CRITICAL --exit-code 1 \
		$(TITAN_IMAGE)

# SEC-102: the committed lockfiles must be in sync with the manifests — a drifted
# lockfile means an unreviewed dependency change slipped in. Re-resolving must
# produce zero diff.
lockfile-drift:
	cd $(API_DIR) && uv lock --check
	cd $(WEB_DIR) && npm ci --ignore-scripts --dry-run >/dev/null

# SEC-110: OWASP compliance-map ↔ suite consistency (removing a mapped test fails).
check-compliance:
	cd $(API_DIR) && uv run python scripts/check_compliance_map.py

# SEC-100: fail on any expired/malformed security-scan exception.
check-exceptions:
	cd $(API_DIR) && uv run python scripts/check_scan_exceptions.py

# SEC-131: every finding in the register must trace to a live regression test.
check-redteam-register:
	cd $(API_DIR) && uv run python scripts/check_redteam_findings.py

# OPS-013: migration safety — real downgrades + no unbounded ACCESS EXCLUSIVE locks.
check-migrations:
	cd $(API_DIR) && uv run python scripts/check_migrations.py

# OPS-005: deploy manifests must pin @sha256: digests, never floating tags.
check-deploy-digests:
	cd $(API_DIR) && uv run python scripts/check_deploy_digests.py

# OPS-017: every paging alert carries a runbook_url; SLO alerts are multiwindow.
check-alert-rules:
	cd $(API_DIR) && uv run python scripts/check_alert_rules.py

# OPS-025: every alert runbook_url resolves to an existing ops/runbooks anchor.
check-runbook-links:
	cd $(API_DIR) && uv run python scripts/check_runbook_links.py

# OPS-002/005/007: K8s manifest security posture (no floating images; datastores are
# unreachable from the edge). Pure Python, no cluster — gates every PR.
check-k8s:
	cd $(API_DIR) && uv run python scripts/check_k8s.py

# EVO-001/002 (Vol 10 §2/§6.1, Phase 83): the prompt registry is pinned, approved,
# and drift-free — every core agent has a manifest entry whose sha256 matches the
# on-disk bytes and carries an approver, no orphan prompts, valid front-matter.
check-evolution:
	cd $(API_DIR) && uv run python scripts/check_evolution.py

# EVO-017 / ADR-EVO-02 (§6): the offline bench gate — an EP that changes prompts must ship a
# challenger bench report that is non-inferior to the champion on every track (gates.yaml).
check-bench:
	cd $(API_DIR) && uv run python scripts/check_bench.py

# OPS-009 (§4.3): autoscaling consistency — HPA custom metrics are real app gauges, the KEDA
# worker scaler matches the code's stream/group, worker drains, bounds pinned to the spec.
check-autoscaling:
	cd $(API_DIR) && uv run python scripts/check_autoscaling.py

# OPS-010/011 (§5.1): blue/green topology consistency — Deployments color-partitioned, stable/
# preview Services consistent, every autoscaler resolves to a colored Deployment (the F4 fix).
check-bluegreen:
	cd $(API_DIR) && uv run python scripts/check_bluegreen.py

# OPS-010/011: behavioural test of the blue/green scripts (release/rollback/slo-watch) against a
# fake kubectl — atomic single flip, F4 delete-before-scale ordering, auto-rollback + fail-safe.
test-bluegreen-scripts:
	bash ops/scripts/test_release.sh

# OPS-019 (Vol 9 §8.3): the disaster-recovery drill. --dry-run verifies the step order + report
# contract with NO infra; the real (destructive) drill needs TITAN_DR_DRILL_CONFIRM=yes + a cluster.
dr-drill:
	ops/scripts/dr-drill.sh
dr-drill-dryrun:
	bash ops/scripts/dr-drill.sh --dry-run
# Validate the newest DR report against the §8.3 step order + RTO<=4h / RPO<=5min targets.
check-dr-report:
	cd $(API_DIR) && uv run python scripts/check_dr_report.py

# OPS-019 (Vol 9 §2): validate the cloud-agnostic Terraform (fmt + offline init + validate).
# The reference module impl uses only Terraform builtins, so init needs no providers/network.
check-terraform:
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false -input=false
	terraform -chdir=infra/terraform validate

# OPS-002: schema-lint the K8s manifests. Needs kubeconform on PATH (CI installs it);
# `kubectl kustomize` renders base + overlays, CRDs (CNPG/Kyverno) skipped by design.
k8s-validate:
	@command -v kubeconform >/dev/null 2>&1 || { echo "install kubeconform (https://github.com/yannh/kubeconform)"; exit 1; }
	kubectl kustomize infra/k8s/base | kubeconform -strict -ignore-missing-schemas -summary
	kubectl kustomize infra/k8s/overlays/staging | kubeconform -strict -ignore-missing-schemas -summary
	kubectl kustomize infra/k8s/overlays/production | kubeconform -strict -ignore-missing-schemas -summary
	kubectl kustomize infra/k8s/policies | kubeconform -ignore-missing-schemas -summary

# OPS-018: full PITR round trip in one command — backup → destroy DB → PITR-restore to a
# target time → validate ledger consistency → smoke. DESTROYS the target DB and needs
# Postgres+WAL+MinIO, so it is CI/staging only and refuses to run without the confirm env.
pitr-drill:
	TITAN_PITR_DRILL_CONFIRM=yes ops/scripts/pitr-drill.sh

# SEC-130: run the LLM red-team corpus against the real controls; non-zero on a
# regression (an unauthorized tool execution or a secret-bearing gateway payload).
redteam-run:
	cd $(API_DIR) && uv run python -m titan.cli redteam run

# SEC-140: verify the §15 secure-defaults checklist against this install.
security-check:
	cd $(API_DIR) && uv run python -m titan.cli security check

supply-chain: sast-python audit-python audit-web osv-scan check-compliance check-exceptions lockfile-drift

# --env-file pins the repo-root .env so host-side runs always target THIS
# project's containers (ports may be remapped there), never defaults.
migrate:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env alembic upgrade head

seed:
	cd $(API_DIR) && uv run --env-file $(CURDIR)/.env python scripts/seed.py

# Vol 9 §5.3 (TITAN-OPS-012): CI warns on feature flags older than 90 days —
# release/experiment flags are debt if they linger (ops kill switches are exempt).
# Advisory (exit 0); set TITAN_FLAGS_STRICT=1 to make it blocking.
check-stale-flags:
	cd $(API_DIR) && uv run $(ENV_FILE_FLAG) python scripts/check_stale_flags.py

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
