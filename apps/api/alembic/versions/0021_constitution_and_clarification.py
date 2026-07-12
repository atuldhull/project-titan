"""Constitution versions + CEO clarification batches, and the G5 constitution
stamp on the gate ledger (Vol 6 §6, §2.4 TITAN-WFL-007/008; §2.3 H5/G5).

Phase 43 adds:

- ``constitution_versions`` — the versioned engineering-standards constitution
  (§6) as a committed data artifact. Seeded here with version 1 = the verbatim §6
  text; a revision is a NEW row with the next ``version`` (append-only via the
  shared ``reject_decision_mutation()`` trigger). Agents retrieve the active
  version into context; gate G5 records which version it judged against.
- ``gate_evaluations.constitution_version`` — the version a design gate (G5)
  evaluated the ArchitecturePackage against, so every G5 ledger row is auditable
  to a specific committed constitution (§2.3 H5 DoD). Null for non-G5 gates.
- ``clarification_batches`` + ``clarification_questions`` — the CEO clarification
  engine (§2.4). At most one open batch per project (UNIQUE on project_id),
  holding ≤5 essential questions and the logged, user-overridable assumptions;
  a non-blocking deadline (default 24h) after which an unanswered batch proceeds
  with best-judgment assumptions (TITAN-WFL-008). These are mutable workflow state
  (answers arrive, status transitions) so they carry the ``set_updated_at`` trigger.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0021
Revises: 0020
"""

import hashlib
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The verbatim Volume 6 §6 engineering-standards constitution, committed as
# version 1. Embedded here (not read from a file) so this migration is a stable
# historical snapshot: replaying it always seeds byte-identical content, and the
# checksum below is computed from exactly this string.
_CONSTITUTION_V1_TITLE = "Engineering Standards Constitution"
_CONSTITUTION_V1_SOURCE = "Volume 6 §6"
_CONSTITUTION_V1_BODY = """## 6. The Engineering Standards Constitution

This section is the **constitution**: every rule here binds all TITAN-generated code, in every project, in every language. Agents receive it verbatim in their system context (retrieved per Volume 5); the orchestrator enforces the machine-checkable subset in CI.

### 6.1 Architecture Standards

**TITAN-WFL-022 (MUST) — Clean Architecture layering.** Generated services SHALL follow Clean Architecture: `domain` (entities, invariants — zero framework imports) ← `application` (use cases, ports) ← `infrastructure` (SQLAlchemy repositories, Redis clients, external APIs) ← `interface` (FastAPI routers, CLI, WebSocket handlers). Dependencies point inward only; import-linter (Python) / eslint-boundaries (TypeScript) rules enforce it in CI. *Acceptance:* an import from `domain` into `infrastructure` fails CI.

**TITAN-WFL-023 (MUST) — SOLID.** All generated code SHALL satisfy SOLID: single responsibility per module/class; extension via interfaces/protocols not modification; substitutable abstractions; narrow role-specific interfaces; dependencies injected against abstractions (FastAPI `Depends` / constructor injection — no service-locator, no module-level singletons holding I/O clients). *Acceptance:* review checklist items for each principle; violations are `major` findings per 6.6.

**TITAN-WFL-024 (MUST) — DDD where appropriate.** Projects with non-trivial domain logic (more than CRUD over ≤3 entities) SHALL use tactical DDD: ubiquitous language from the ProductBacklog, aggregates guarding invariants, domain events for cross-aggregate effects, bounded contexts mapped to modules. CRUD-only projects SHALL NOT be forced into DDD ceremony — the Architect records the classification in the ArchitecturePackage. *Acceptance:* the ArchitecturePackage contains an explicit `domain_complexity` classification with rationale.

**TITAN-WFL-025 (MUST) — Repository pattern.** All persistence access SHALL go through repository interfaces defined in `application` and implemented in `infrastructure` with SQLAlchemy 2.x (async). No ORM queries in routers or use cases. *Acceptance:* grep gate — `sqlalchemy` imports outside `infrastructure` fail CI.

**TITAN-WFL-026 (MUST) — CQRS only if justified.** CQRS SHALL NOT be the default. It is permitted only when the Architect's ADR demonstrates at least one of: (a) read/write load asymmetry >10×, (b) materially divergent read models, (c) event-sourcing already justified for audit. Absent the ADR, standard use-case + repository flow applies. *Acceptance:* any `commands/`/`queries/` split without a linked ADR is a `blocking` review finding.

### 6.2 Naming Conventions

| Scope | Convention | Example |
|---|---|---|
| Python modules/functions/vars | `snake_case` | `create_invoice.py`, `def apply_discount()` |
| Python classes | `PascalCase`; suffix by role | `InvoiceRepository`, `CreateInvoiceUseCase` |
| TypeScript files | `kebab-case.ts(x)`; components `PascalCase` export | `invoice-table.tsx` → `InvoiceTable` |
| TS variables/functions | `camelCase`; hooks `useX` | `useInvoiceFilters` |
| Constants (both) | `SCREAMING_SNAKE_CASE` | `MAX_RETRY_COUNT` |
| Database | `snake_case`; plural tables; `id` PK; `<table>_id` FKs; indexes `ix_<table>_<cols>` | `invoices`, `ix_invoices_customer_id` |
| REST endpoints | plural nouns, kebab-case, versioned prefix | `GET /api/v1/invoices/{invoice_id}` |
| Events (Redis Streams) | `domain.entity.action` past tense | `billing.invoice.created` |
| Git branches | `type/task-id-slug` | `feat/TSK-142-invoice-pdf` |
| Commits | Conventional Commits | `feat(billing): generate invoice PDFs` |

**TITAN-WFL-027 (MUST) — Machine-enforced naming.** The conventions above SHALL be enforced by Ruff, ESLint, a SQL migration linter, and a commit-message hook; violations fail CI. *Acceptance:* a camelCase Python function or a non-conventional commit message fails the pipeline before review.

### 6.3 Error Handling

**TITAN-WFL-028 (MUST) — Error taxonomy and policy.** Every service SHALL define a domain error hierarchy (`DomainError` → e.g., `NotFoundError`, `ConflictError`, `ValidationError`, `AuthorizationError`) raised from domain/application layers and translated exactly once, at the interface layer, into RFC 9457 problem-details responses with stable machine-readable `type` codes. Rules: no bare `except:`; no swallowed exceptions (catch → log-and-rethrow or handle, never pass); no stack traces or internal identifiers in client responses; all external I/O wrapped with timeouts and typed failure mapping; retries with exponential backoff + jitter only on idempotent operations. *Acceptance:* the error-handling test suite verifies every endpoint returns problem-details on induced failure and that logs contain the stack trace the client response omits.

### 6.4 Logging

**TITAN-WFL-029 (MUST) — Structured, correlated logging.** All logs SHALL be structured JSON (via `structlog`/`pino`-equivalent) shipped to Loki, with mandatory fields: `timestamp`, `level`, `service`, `trace_id`/`span_id` (OpenTelemetry-propagated), `project_id`, `agent_id` where applicable, and `event`. Levels: `DEBUG` (dev-only), `INFO` (state changes), `WARNING` (handled anomalies), `ERROR` (failed operations), `CRITICAL` (data-loss/security). Secrets, tokens, and PII SHALL never be logged — enforced by a redaction filter and a CI log-call scanner. *Acceptance:* every request's logs are joinable to its trace in Grafana by `trace_id`; the redaction test suite passes.

### 6.5 Testing Strategy & Coverage Gates

```
            TEST PYRAMID + GATES
        ┌───────────────┐
        │  e2e (Playwr.)│  critical user journeys: 100% covered
        ├───────────────┤
        │  integration  │  every public API endpoint + every
        │ (testcontain.)│  repository against real PG16/Redis7
        ├───────────────┤
        │     unit      │  ≥80% line / ≥70% branch on changed code
        └───────────────┘
   + performance (k6): p95 budgets per endpoint
   + security: SAST, dependency & secret scans (every PR)
```

**TITAN-WFL-030 (MUST) — Test layers and gates.** Every component SHALL ship with: unit tests (pytest / Vitest) written in the same PR as the code; integration tests running against real PostgreSQL 16 and Redis 7 via testcontainers; e2e tests (Playwright) for every critical user journey named in the ProductBacklog; performance tests (k6) for endpoints with declared budgets; security scans per H8. Coverage gates, enforced in CI on the diff: **≥80% line and ≥70% branch coverage of changed code; 100% of new public endpoints integration-tested; 100% of critical journeys e2e-tested before M2.** Coverage may never decrease project-wide. *Acceptance:* a PR dropping diff coverage below gate values cannot merge; the QA report at M2 shows the journey matrix fully green.

**TITAN-WFL-031 (MUST) — Deterministic tests.** Tests SHALL be hermetic: no network calls to real external services (use fakes/contract stubs), no time/random flakiness (injected clocks and seeds), parallel-safe. A test failing intermittently is quarantined within one CI run, ticketed as a P1 defect, and counted in the debt ledger. *Acceptance:* the flake log in QAReports trends to zero across benchmark projects; quarantined tests block M3 until resolved.

### 6.6 Code Review Rules & Rejection Criteria

The Code Reviewer Agent's authority is real: it can and must reject. Verdicts: `APPROVED`, `APPROVED_WITH_COMMENTS` (minor findings → debt tickets), `REJECTED`.

**TITAN-WFL-032 (MUST) — Mandatory rejection criteria.** A PR SHALL be `REJECTED` if any of the following hold:

| # | Rejection criterion | Severity |
|---|---|---|
| R1 | Violates Clean Architecture boundaries (6.1) or SOLID at module level | blocking |
| R2 | Missing or failing tests; diff coverage below gates (6.5) | blocking |
| R3 | Any high/critical security finding, hardcoded secret, or missing input validation on external input | blocking |
| R4 | Duplicated logic (>10 similar lines) where an existing abstraction applies | blocking |
| R5 | Swallowed errors, bare excepts, or client-visible stack traces (6.3) | blocking |
| R6 | Unjustified pattern use — CQRS/event-sourcing without ADR (6.1) | blocking |
| R7 | Placeholder code: TODO/FIXME, stubbed function bodies, commented-out code | blocking |
| R8 | Public API change without OpenAPI update + versioning per ArchitecturePackage | blocking |
| R9 | Performance budget exceeded or N+1 query introduced | blocking |
| R10 | Naming/lint violations that somehow bypassed CI | major |
| R11 | Missing docstrings/JSDoc on public functions; missing migration rollback | major |

Two `major` findings equal one blocking condition. *Acceptance:* the reviewer prompt-regression suite includes fixture PRs for R1–R11 and the reviewer rejects each with the correct code.

**TITAN-WFL-033 (MUST) — Review scope discipline.** Reviews SHALL address the diff plus its blast radius only; reviewers SHALL NOT demand unrelated refactors (those become debt/backlog proposals). Every finding cites file, line, violated rule ID, and a concrete fix. *Acceptance:* 100% of findings in loop records carry rule citations; findings outside the diff's blast radius are marked `advisory` and never block.

### 6.7 Documentation Requirements

**TITAN-WFL-034 (MUST) — Documentation as a gated deliverable.** Every project SHALL ship: README with a copy-paste Docker Compose quickstart verified by execution (G11); architecture guide generated from the ArchitecturePackage; API reference from OpenAPI with prose descriptions on every endpoint, parameter, and error `type`; user manual keyed to the ProductBacklog journeys; deployment runbook (including rollback); contribution guide; troubleshooting guide seeded from defects encountered during the build; ADR index. Code-level: public functions/classes carry docstrings (Google style) / JSDoc; modules carry purpose headers; complex algorithms carry rationale comments — comments explain *why*, never narrate *what*. *Acceptance:* the G11 coverage checker reports 100% on endpoints and ADR indexing, and the quickstart executes cleanly in CI."""


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def upgrade() -> None:
    # -- §6: the versioned constitution artifact (append-only) -------------------
    op.create_table(
        "constitution_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("version", name="uq_constitution_versions_version"),
    )
    # A committed version is immutable; revisions are new rows (TITAN-WFL-004 spirit).
    op.execute(
        "CREATE TRIGGER trg_constitution_versions_append_only "
        "BEFORE UPDATE OR DELETE ON constitution_versions "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )
    # Seed version 1 = the verbatim §6 text; checksum is sha256 of exactly this body.
    checksum = hashlib.sha256(_CONSTITUTION_V1_BODY.encode("utf-8")).hexdigest()
    op.execute(
        sa.text(
            "INSERT INTO constitution_versions "
            "(id, version, title, body, checksum, source_ref) "
            "VALUES (:id, 1, :title, :body, :checksum, :source_ref)"
        ).bindparams(
            id=uuid.uuid4(),
            title=_CONSTITUTION_V1_TITLE,
            body=_CONSTITUTION_V1_BODY,
            checksum=checksum,
            source_ref=_CONSTITUTION_V1_SOURCE,
        )
    )

    # -- §2.3 H5/G5: the constitution version a design gate judged against --------
    op.add_column(
        "gate_evaluations",
        sa.Column("constitution_version", sa.Integer, nullable=True),
    )

    # -- §2.4: CEO clarification batches (≤1 open batch per project) --------------
    op.create_table(
        "clarification_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # open | answered | timed_out
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("timeout_hours", sa.Integer, nullable=False, server_default=sa.text("24")),
        # Non-blocking deadline (TITAN-WFL-008): after this, proceed with assumptions.
        sa.Column("deadline_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
        # answered | timeout_best_judgment
        sa.Column("resolution", sa.Text, nullable=True),
        # Logged, user-overridable assumptions (TITAN-WFL-007), flagged on timeout.
        sa.Column("assumptions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("opened_by", sa.Text, nullable=False),
        *_conventions(),
        # One clarification batch per project (single-batch policy, TITAN-WFL-007).
        sa.UniqueConstraint("project_id", name="uq_clarification_batches_project"),
    )
    op.execute(
        "CREATE TRIGGER trg_clarification_batches_updated_at "
        "BEFORE UPDATE ON clarification_batches "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "clarification_questions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clarification_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("answered_at", TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
        # The UNIQUE (batch_id, ordinal) constraint's btree index already serves the
        # only read (questions_for_batch: batch_id equality + ordinal ordering), so
        # no separate index is created (it would be a redundant duplicate).
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_clarification_questions_batch_ordinal"),
    )
    op.execute(
        "CREATE TRIGGER trg_clarification_questions_updated_at "
        "BEFORE UPDATE ON clarification_questions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clarification_questions_updated_at ON clarification_questions"
    )
    op.drop_table("clarification_questions")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clarification_batches_updated_at ON clarification_batches"
    )
    op.drop_table("clarification_batches")
    op.drop_column("gate_evaluations", "constitution_version")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_constitution_versions_append_only ON constitution_versions"
    )
    op.drop_table("constitution_versions")
