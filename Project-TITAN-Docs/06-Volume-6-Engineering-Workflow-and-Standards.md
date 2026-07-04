# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 6 — Autonomous Engineering Workflow & Standards

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform (10 volumes) |
| **Volume** | 6 — Autonomous Engineering Workflow & Standards |
| **Doc ID** | TITAN-VOL-06 |
| **Requirement Prefix** | TITAN-WFL-NNN |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |

**Abstract.** This volume specifies how TITAN turns a one-sentence user request into deployed, documented, release-approved software. It defines the full delivery pipeline — User Request → CEO Agent → Product Manager → Project Manager → Architect → Backend/Frontend/Database Engineers → QA → Security → Code Reviewer → DevOps → Documentation → Release Approval — with every handoff specified as a contract (trigger, artifact, acceptance gate, failure path); the per-component self-improvement loop (Implementation → Review → Critique → Refactor → Testing → Security Scan → Performance Analysis → Approval → Merge) with bounded iteration limits; sprint and milestone mechanics; the auto-generated project artifacts; and the engineering-standards constitution all TITAN-generated code must obey. Everything runs on the canonical stack fixed in Volume 1 (Python 3.12 + FastAPI orchestrator, PostgreSQL 16, Redis 7 Streams, Qdrant, Next.js 15 dashboard), enforced by the orchestrator of Volumes 2–3.

---

## Table of Contents

- 1. Introduction & Scope
- 2. The Delivery Pipeline
  - 2.1 Pipeline Overview
  - 2.2 The Handoff Contract
  - 2.3 Handoff Specifications (H1–H12)
  - 2.4 CEO Clarification Policy
  - 2.5 Task State Machine
- 3. The Self-Improvement Loop
  - 3.1 Loop Overview
  - 3.2 Stage Specifications
  - 3.3 Iteration Bounds & Escalation
- 4. Sprint Planning & Milestone Mechanics
  - 4.1 Sprint Lifecycle
  - 4.2 Milestones & Release Trains
  - 4.3 Blocker Detection & Recovery
- 5. Auto-Generated Project Artifacts
- 6. The Engineering Standards Constitution
  - 6.1 Architecture Standards
  - 6.2 Naming Conventions
  - 6.3 Error Handling
  - 6.4 Logging
  - 6.5 Testing Strategy & Coverage Gates
  - 6.6 Code Review Rules & Rejection Criteria
  - 6.7 Documentation Requirements
- 7. Decision Records
- 8. Risks & Open Questions
- 9. Cross-References

---

## 1. Introduction & Scope

TITAN is an organization of agents, not a single model. This volume is that organization's **operating manual** — the fixed process every project moves through — and its **constitution** — the non-negotiable quality rules every generated line of code must satisfy.

Two design principles govern everything below:

1. **Every transition is a contract.** Work moves between agents only through schema-validated artifacts passing explicit acceptance gates. Every handoff is a persisted, replayable event on the Redis Streams task queue (NATS JetStream at scale-out, per Volume 1), with the artifact stored in PostgreSQL and indexed in Qdrant.
2. **First drafts are never accepted.** Every component passes the bounded self-improvement loop (Section 3) before merge. Autonomy without gates produces slop; gates without bounds produce livelock. This volume fixes both bounds.

**In scope:** pipeline stages, handoff contracts, the review loop, sprint/milestone mechanics, auto-generated artifacts, and the code-quality constitution. **Out of scope:** agent prompt design and internal decision trees (Volume 2), orchestrator implementation (Volume 3), UI for the workflow (Volume 4), memory/RAG internals (Volume 5), external tool bindings (Volume 7), threat model (Volume 8), production CI/CD internals (Volume 9).

### 1.1 Requirement Conventions

Requirements use IDs `TITAN-WFL-NNN` with priority **MUST** (release-blocking), **SHOULD** (deviation requires a logged ADR), or **MAY** (optional). Each carries acceptance criteria testable against the orchestrator.

---

## 2. The Delivery Pipeline

### 2.1 Pipeline Overview

```
                    TITAN DELIVERY PIPELINE (macro flow)
 ┌──────────────┐
 │ USER REQUEST │  "Build me a Netflix clone."
 └──────┬───────┘
        v
 ┌──────────────┐  clarify (max 5 essential questions)   ┌─────────────┐
 │  CEO AGENT   │<--------------------------------------->│    USER     │
 │ vision/prior.│                                          └─────────────┘
 └──────┬───────┘
        v  H2: Project Brief ─────────────[G2: brief complete?]── fail → re-clarify
 ┌──────────────┐
 │  PM AGENT    │  user stories + acceptance criteria
 └──────┬───────┘
        v  H3: Product Backlog ───────────[G3: INVEST + AC testable?]── fail → PM rework
 ┌──────────────┐
 │ PROJECT MGR  │  tasks, estimates, sprints, dependencies
 └──────┬───────┘
        v  H4: Sprint Plan + Task Graph ──[G4: DAG acyclic, capacity ok?]── fail → replan
 ┌──────────────┐
 │  ARCHITECT   │  architecture package, API contracts, schema
 └──────┬───────┘
        v  H5: Architecture Package ──────[G5: constitution-compliant?]── fail → redesign
 ┌──────┴───────────────────────────────┐
 │        ENGINEERING (parallel)        │   each task runs the
 │  ┌─────────┐ ┌──────────┐ ┌───────┐  │   SELF-IMPROVEMENT LOOP
 │  │ BACKEND │ │ FRONTEND │ │  DB   │  │   (Section 3) before exit
 │  └─────────┘ └──────────┘ └───────┘  │
 └──────┬───────────────────────────────┘
        v  H6: Merge Candidates ─────────[G6: loop passed, CI green?]── fail → back to loop
 ┌──────────────┐
 │   QA AGENT   │  integration + e2e + regression on assembled build
 └──────┬───────┘
        v  H7: QA Report ────────────────[G7: 0 blocking defects?]── fail → defect tickets
 ┌──────────────┐
 │   SECURITY   │  audit, dependency scan, secret scan, OWASP
 └──────┬───────┘
        v  H8: Security Clearance ───────[G8: 0 high/critical?]── fail → security tickets
 ┌──────────────┐
 │CODE REVIEWER │  release-level review of the integrated diff
 └──────┬───────┘
        v  H9: Approved Release Branch ──[G9: review approved?]── fail → rework tickets
 ┌──────────────┐
 │    DEVOPS    │  build, deploy to staging → smoke test → prod
 └──────┬───────┘
        v  H10: Deployment Record ───────[G10: smoke + health green?]── fail → rollback
 ┌──────────────┐
 │DOCUMENTATION │  README, API docs, user manual, release notes
 └──────┬───────┘
        v  H11: Doc Bundle ──────────────[G11: doc coverage 100%?]── fail → doc rework
 ┌──────────────┐
 │ CEO RELEASE  │  final approval against original vision
 │   APPROVAL   │
 └──────┬───────┘
        v  H12: Release + Handover to USER
```

**TITAN-WFL-001 (MUST) — Pipeline as persisted state machine.** The pipeline SHALL be implemented as an explicit state machine per project, persisted in PostgreSQL 16, with each stage transition emitted as an event on Redis Streams and mirrored to the dashboard over WebSockets. *Acceptance:* killing and restarting the orchestrator mid-pipeline resumes the project at the exact stage and iteration count; the event log reconstructs the full history.

**TITAN-WFL-002 (MUST) — No stage skipping.** No stage may be skipped or reordered; a stage may only be marked `NOT_APPLICABLE` by the CEO Agent with a logged justification (e.g., a docs-only change skipping DB engineering). *Acceptance:* attempting an out-of-order transition raises a rejected-transition event and leaves state unchanged.

### 2.2 The Handoff Contract

Every handoff Hn is a 4-tuple. This is the universal protocol; Section 2.3 instantiates it twelve times.

| Element | Definition | Enforcement |
|---|---|---|
| **Trigger** | The event that starts the handoff (producer marks its stage `COMPLETE`) | Redis Streams message `pipeline.<project>.stage_complete` |
| **Artifact** | A versioned, Pydantic-v2-validated JSON/Markdown document | Stored in PostgreSQL (`artifacts` table), embedded into Qdrant |
| **Acceptance gate** | Machine-checkable predicate + (where needed) consumer-agent review | Gate evaluator in orchestrator; result persisted with reasons |
| **Failure path** | Where work routes on gate failure, with retry budget | Bounded retries (default 3) then human escalation |

**TITAN-WFL-003 (MUST) — Schema-validated artifacts.** Every handoff artifact SHALL validate against a versioned Pydantic v2 schema before the gate is evaluated; schema validation failure counts as gate failure. *Acceptance:* an artifact missing a required field is rejected with a field-level error report attached to the failure event.

**TITAN-WFL-004 (MUST) — Auditable gates.** Every gate evaluation SHALL be persisted with verdict, evaluator identity (agent + model + prompt version), reasons, and timestamps, and SHALL be visible in the dashboard audit trail. *Acceptance:* for any release, a complete gate ledger can be exported showing every pass/fail with reasons.

**TITAN-WFL-005 (MUST) — Bounded failure paths.** Every gate SHALL have a retry budget (default 3 per stage, configurable per project). On budget exhaustion the orchestrator SHALL pause the pipeline, mark the project `NEEDS_HUMAN`, and notify the user with a structured escalation report (what failed, all attempts, agent hypotheses). *Acceptance:* a stage forced to fail 4 times produces exactly one escalation and zero further LLM calls for that stage.

### 2.3 Handoff Specifications (H1–H12)

**H1 — User → CEO Agent.**
*Trigger:* user submits a request via dashboard or API. *Artifact:* `UserRequest` (raw text, attachments, org context). *Gate G1:* request is non-empty and not a duplicate of an active project. *Failure path:* malformed requests are rejected at the API layer; ambiguity is not failure — it triggers the clarification policy (2.4).

**H2 — CEO → Product Manager.**
*Trigger:* CEO marks vision analysis complete. *Artifact:* `ProjectBrief` — product vision (≤300 words), target users, prioritized feature list (MoSCoW), explicit non-goals, constraints (budget/local-first/compliance), success criteria. *Gate G2:* every MUST-feature has a measurable success criterion; non-goals section non-empty; constraints include the canonical stack unless the user overrode it. *Failure path:* PM Agent returns the brief with gap annotations; CEO revises (retries count against CEO stage budget).

**H3 — PM → Project Manager.**
*Trigger:* PM marks backlog complete. *Artifact:* `ProductBacklog` — user stories in "As a…, I want…, so that…" form, each with Gherkin-style acceptance criteria (Given/When/Then), priority, and story-point estimate. *Gate G3:* every story satisfies INVEST (checked by rubric prompt + lint rules: has actor, has outcome, ≤13 points, ≥1 testable AC); every MUST-feature from the brief is covered by ≥1 story (traceability check). *Failure path:* failing stories return to PM with the specific INVEST violations; passing stories proceed (partial acceptance is allowed at G3 only).

**H4 — Project Manager → Architect.**
*Trigger:* Project Manager marks planning complete. *Artifact:* `SprintPlan` + `TaskGraph` — tasks decomposed from stories (each ≤1 ideal agent-day), assignee role, dependencies as a DAG, sprint assignment, milestone mapping. *Gate G4:* task DAG is acyclic; every story maps to ≥1 task; sprint 1 contains a walking-skeleton slice (thin end-to-end path); no sprint exceeds configured capacity. *Failure path:* cycle or capacity violations return to Project Manager with the offending subgraph highlighted.

**H5 — Architect → Engineers.**
*Trigger:* Architect marks design complete. *Artifact:* `ArchitecturePackage` — component diagram (ASCII + structured JSON), module boundaries mapped to Clean Architecture layers, OpenAPI contract for every backend endpoint, database schema draft (tables, indexes, migration order), frontend route map and state-management plan, ADRs for every non-default choice. *Gate G5:* package complies with the constitution (Section 6) — verified by checklist evaluation; every task in the TaskGraph references a component in the package; API contracts validate as OpenAPI 3.1. *Failure path:* Code Reviewer Agent (acting as design reviewer) returns numbered objections; Architect revises.

**H6 — Engineers → QA.**
*Trigger:* all tasks of the current sprint scope reach `MERGED` (each having individually passed the Section 3 loop). *Artifact:* `MergeCandidateManifest` — list of merged PRs, changelog, updated dependency lockfiles, migration scripts. *Gate G6:* CI is green on the integration branch (lint, typecheck, unit + integration suites, coverage gates per 6.5); all migrations apply cleanly to a fresh PostgreSQL 16 and to a snapshot of the previous schema. *Failure path:* failing components reopen as tasks and re-enter the self-improvement loop at the Refactor stage with the CI failure attached as critique input.

**H7 — QA → Security.**
*Trigger:* QA completes its pass on the assembled build. *Artifact:* `QAReport` — executed test matrix (integration, e2e via Playwright, regression, performance baseline), defects classified P0–P3, flake log. *Gate G7:* zero P0/P1 defects; e2e critical-path suite 100% passing; regression suite passing. *Failure path:* P0/P1 defects become tasks routed to the owning engineer role; the pipeline holds at QA and re-runs only affected suites on fix (retry budget applies to full QA cycles, not individual tests).

**H8 — Security → Code Reviewer.**
*Trigger:* Security Agent completes audit. *Artifact:* `SecurityClearance` — SAST results, dependency audit (lockfile CVE scan), secret scan, OWASP Top-10 checklist with per-item evidence, auth/RBAC verification against Volume 8 policy. *Gate G8:* zero high/critical findings; mediums have logged accepted-risk entries approved by CEO Agent or remediation tasks scheduled within the release. *Failure path:* findings become security-labeled tasks with mandatory loop re-entry; a repeated identical finding after fix attempts escalates immediately (does not consume full retry budget).

**H9 — Code Reviewer → DevOps.**
*Trigger:* Security clearance issued. *Artifact:* `ReleaseReview` — release-level review of the full integrated diff against the constitution: architecture-boundary check, duplication scan, public-API consistency, changelog accuracy. *Gate G9:* Reviewer verdict `APPROVED` (rejection criteria in 6.6 apply at release scope too). *Failure path:* `REJECTED` produces itemized rework tickets; `APPROVED_WITH_COMMENTS` proceeds but logs comments as technical-debt entries (Section 5).

**H10 — DevOps → Documentation.**
*Trigger:* release review approved. *Artifact:* `DeploymentRecord` — image digests, environment (staging→production per Volume 9 promotion rules; Docker Compose for local-first deployments), applied migrations, smoke-test results, health-check status, Grafana dashboard links, rollback pointer. *Gate G10:* staging smoke suite green; production health checks green for the soak window (default 15 min); OpenTelemetry traces flowing. *Failure path:* automatic rollback to the rollback pointer, incident event emitted, defect task created; pipeline returns to H6 scope for the fix.

**H11 — Documentation → CEO.**
*Trigger:* deployment stable. *Artifact:* `DocBundle` — README, architecture guide, API reference (generated from OpenAPI + prose), user manual, deployment/runbook, contribution guide, troubleshooting guide, release notes, updated decision-record index. *Gate G11:* documentation coverage checks pass (6.7): every public endpoint documented, every ADR indexed, README quickstart executes successfully in a clean Docker Compose environment. *Failure path:* Documentation Agent revises with the specific coverage failures; a quickstart that fails to execute is treated as a P1 defect, not a docs issue, and routes to H7.

**H12 — CEO → User (Release Approval).**
*Trigger:* doc bundle accepted. *Artifact:* `ReleaseApproval` — CEO's verification that the release satisfies the original `ProjectBrief` success criteria, with a traceability table (brief feature → stories → tasks → PRs → tests → docs). *Gate G12:* every MUST-feature traced to passing acceptance tests; user notified with release summary and access details. *Failure path:* unmet criteria reopen as backlog items for the next cycle; the CEO MUST NOT silently narrow scope — scope changes require user confirmation.

**TITAN-WFL-006 (MUST) — Full traceability.** Every release SHALL carry an automatically generated traceability table from brief features through stories, tasks, PRs, tests, and docs. *Acceptance:* selecting any MUST-feature in the dashboard shows its complete chain with no gaps.

### 2.4 CEO Clarification Policy

The CEO Agent asks **only essential questions** — those whose answers change architecture, scope, or acceptance criteria. Everything else is decided autonomously using canonical defaults and logged as an assumption.

**TITAN-WFL-007 (MUST) — Essential-questions-only.** The CEO Agent SHALL ask at most **5 clarification questions per project**, in a single batch, and only when the answer is (a) not inferable from the request or organization memory, (b) not covered by a canonical default, and (c) materially changes the `ProjectBrief`. All other unknowns SHALL be resolved as logged assumptions in the brief's `assumptions` section, each marked user-overridable. *Acceptance:* for the request "Build me a Netflix clone," the CEO produces ≤5 questions (e.g., payment integration real or mocked? content licensing scope?) and ≥10 logged assumptions; asking a question answerable from the canonical stack (e.g., "which database?") is a policy violation flagged by the prompt-regression suite.

**TITAN-WFL-008 (MUST) — Non-blocking clarification.** If the user does not answer within a configurable window (default 24h), the CEO SHALL proceed with best-judgment answers recorded as high-visibility assumptions, and the release approval (H12) SHALL restate them. *Acceptance:* an unanswered project still reaches release with all assumption-driven decisions flagged in the release summary.

### 2.5 Task State Machine

```
             ┌────────┐
             │BACKLOG │
             └───┬────┘
                 v
 ┌────────┐  ┌────────┐   ┌─────────────┐   ┌────────┐   ┌────────┐
 │BLOCKED │<─┤ READY  ├──>│ IN_PROGRESS ├──>│IN_LOOP │──>│ MERGED │
 └───┬────┘  └────────┘   └──────┬──────┘   └───┬────┘   └───┬────┘
     │            ^              │              │            v
     └────────────┘              v              v        ┌────────┐
                            ┌────────┐     ┌─────────┐   │ DONE   │
                            │ FAILED │────>│ESCALATED│   └───┬────┘
                            └────────┘     └─────────┘       v
                                                        (REOPENED → READY
                                                         if defect found)
```

**TITAN-WFL-009 (MUST) — Task states.** Tasks SHALL move only along the edges above; `IN_LOOP` denotes the self-improvement loop; `DONE` requires the parent story's acceptance criteria to pass; any state change SHALL emit an event consumed by the Kanban artifact (Section 5). *Acceptance:* an illegal transition (e.g., `BACKLOG → MERGED`) is rejected and logged.

---

## 3. The Self-Improvement Loop

The pipeline in Section 2 is the **macro** flow between agents. The self-improvement loop is the **micro** flow inside every engineering task: no component ships as a first draft.

### 3.1 Loop Overview

```
        SELF-IMPROVEMENT LOOP (runs per component/task, max 3 cycles)

   ┌────────────────┐
   │ IMPLEMENTATION │  engineer agent writes code + unit tests
   └───────┬────────┘
           v
   ┌────────────────┐   ┌────────────────┐
   │     REVIEW     ├──>│    CRITIQUE    │  reviewer agent: findings list,
   └────────────────┘   └───────┬────────┘  each finding = (severity, file,
           ^                    v            line, rationale, suggestion)
           │            ┌────────────────┐
           │            │    REFACTOR    │  engineer applies/refutes each
           │            └───────┬────────┘  finding with written response
           │                    v
           │            ┌────────────────┐
           │            │    TESTING     │  full suite + new tests for
           │            └───────┬────────┘  every fixed finding
           │                    v
           │            ┌────────────────┐
           │            │ SECURITY SCAN  │  SAST + deps + secrets (task scope)
           │            └───────┬────────┘
           │                    v
           │            ┌────────────────┐
           │            │ PERF ANALYSIS  │  budgets: p95 latency, N+1 scan,
           │            └───────┬────────┘  bundle size (frontend)
           │                    v
           │              ┌───────────┐    ┌──────────┐    ┌───────┐
           └──── fail ────┤ APPROVAL? ├───>│ APPROVED │───>│ MERGE │
             (cycle++)    └─────┬─────┘    └──────────┘    └───────┘
                                │ 3 cycles exhausted
                                v
                          ┌───────────┐
                          │ ESCALATED │  → Architect arbitration → human
                          └───────────┘
```

**TITAN-WFL-010 (MUST) — Mandatory loop, no first drafts.** Every component SHALL complete ≥1 full loop cycle before merge; direct merge of an unreviewed draft SHALL be technically impossible (branch protection + orchestrator gate). *Acceptance:* a PR without an attached loop record cannot reach `MERGED` in the task state machine.

### 3.2 Stage Specifications

| Stage | Actor | Output artifact | Pass condition |
|---|---|---|---|
| Implementation | Owning engineer agent | Branch + PR + unit tests | Compiles; lint/typecheck clean; tests written alongside code |
| Review | Code Reviewer Agent | `ReviewFindings` (typed list) | N/A (always produces findings; empty list allowed from cycle 2) |
| Critique | Reviewer + QA + Security perspectives | Consolidated critique, deduplicated, severity-ranked | Every finding actionable: file, line, rationale, suggested fix |
| Refactor | Owning engineer agent | Updated PR + per-finding response (`fixed`/`refuted` + reason) | No finding ignored silently |
| Testing | QA Agent (task scope) | Test run report | Unit ≥80% line coverage on changed code; all suites green; every `fixed` finding has a covering test |
| Security Scan | Security Agent (task scope) | Scan report | Zero high/critical in changed code and newly added dependencies |
| Perf Analysis | Backend/Frontend engineer + QA | Perf report | Within budgets: API p95 ≤ endpoint budget from ArchitecturePackage; no N+1 queries; frontend route JS budget respected |
| Approval | Code Reviewer Agent | Verdict | `APPROVED` per 6.6 criteria |
| Merge | DevOps Agent (automation) | Merge commit to trunk | CI green post-merge; task → `MERGED` |

**TITAN-WFL-011 (MUST) — Adversarial review separation.** The reviewing agent instance SHALL NOT share conversation context with the implementing agent instance; the reviewer sees only the PR diff, the task spec, the ArchitecturePackage excerpt, and the constitution. *Acceptance:* review requests contain no implementation-side chain-of-thought; verified by message-log inspection in the audit trail.

**TITAN-WFL-012 (MUST) — Refute-or-fix.** Every critique finding SHALL receive an explicit `fixed` or `refuted` response; refutations are re-judged by the reviewer, and an upheld finding refuted twice escalates to the Architect for binding arbitration. *Acceptance:* the loop record for any merged PR shows a terminal disposition for 100% of findings.

### 3.3 Iteration Bounds & Escalation

**TITAN-WFL-013 (MUST) — Bounded loops.** The loop SHALL run at most **3 full cycles** per task (configurable 2–5). On exhaustion: task → `ESCALATED`, Architect reviews and either (a) re-scopes the task, (b) overrides specific findings with a logged ADR, or (c) escalates to the human user. The loop SHALL also terminate early on convergence: two consecutive cycles with zero new findings of severity ≥ minor. *Acceptance:* no task in the system ever records a 4th cycle; convergence termination is logged distinctly from exhaustion.

**TITAN-WFL-014 (SHOULD) — Diminishing-returns detection.** The orchestrator SHOULD detect oscillation (a finding re-introduced after being fixed, or reviewer flip-flopping on the same line) and short-circuit to Architect arbitration rather than burning remaining cycles. *Acceptance:* an A→B→A edit sequence on the same hunk triggers arbitration within the same cycle.

---

## 4. Sprint Planning & Milestone Mechanics

### 4.1 Sprint Lifecycle

TITAN sprints are logical batches, not calendar rituals — agents work continuously — but the sprint construct bounds work-in-progress, orders dependencies, and gives the user review checkpoints.

**TITAN-WFL-015 (MUST) — Auto-planned sprints.** The Project Manager Agent SHALL partition the TaskGraph into sprints such that: sprint 1 is a walking skeleton (thin end-to-end slice touching frontend, backend, database, and deploy); each sprint's tasks respect DAG order; sprint size respects configured parallel-agent capacity; each sprint ends with a potentially releasable increment. *Acceptance:* for any generated plan, no task appears in an earlier sprint than any of its dependencies, and sprint 1 contains ≥1 task per engineering role plus a deploy task.

**TITAN-WFL-016 (MUST) — Sprint ceremonies as agent events.** Each sprint SHALL open with an automated planning record (goals, committed stories, risks) and close with an automated review record (demo links against acceptance criteria, velocity actuals vs. estimates) and a retrospective record (what the agents will change — fed to organization memory per Volume 5). *Acceptance:* every completed sprint has all three records persisted and visible in the dashboard.

**TITAN-WFL-017 (SHOULD) — Estimation feedback.** Story-point estimates SHOULD be recalibrated per project from actuals (tokens, wall-clock, loop cycles consumed), with the correction factor stored in organization memory. *Acceptance:* estimate error (median absolute percentage) decreases across a project's sprints in benchmark runs.

### 4.2 Milestones & Release Trains

| Milestone | Definition of done | Gate owner |
|---|---|---|
| M0 — Inception | ProjectBrief + ProductBacklog accepted (G2, G3) | CEO |
| M1 — Walking Skeleton | Sprint 1 increment deployed to staging; e2e "hello path" green | DevOps |
| M2 — Feature Complete | All MUST-stories `DONE`; G6–G7 passed | QA |
| M3 — Hardened | G8–G9 passed; performance budgets met; debt report reviewed | Security + Reviewer |
| M4 — Released | G10–G12 passed; docs live; user notified | CEO |

**TITAN-WFL-018 (MUST) — Milestone gating.** Milestones SHALL be strictly ordered; a milestone regression (e.g., new P0 after M2) reverts the project to the earlier milestone and reopens the relevant pipeline stage. *Acceptance:* milestone state in the dashboard always equals the highest milestone whose definition of done currently holds.

### 4.3 Blocker Detection & Recovery

**TITAN-WFL-019 (MUST) — Automatic blocker detection.** The Project Manager Agent SHALL flag a task `BLOCKED` when any of: no state change beyond 2× its estimate; its dependency failed; it waits on an unanswered clarification; loop escalation pending. Blockers SHALL trigger: dependency re-check, reassignment to a fresh agent instance with summarized context, or decomposition into smaller tasks — in that order — before human escalation. *Acceptance:* a synthetically stalled task is detected and recovery attempted within one scheduler tick (≤60 s).

---

## 5. Auto-Generated Project Artifacts

All artifacts are derived views over the same PostgreSQL event log — never hand-maintained, never stale.

| Artifact | Source data | Regenerated on | Format |
|---|---|---|---|
| **Roadmap** | Milestones + sprint plan + brief priorities | Any milestone/sprint change | Timeline JSON + Markdown |
| **Kanban board** | Task state machine events | Every task transition | Live board (dashboard, Vol 4) |
| **Dependency graph** | TaskGraph DAG + component graph | Task/architecture change | DOT + rendered view |
| **Release plan** | Milestones + DeploymentRecords + traceability table | Gate G6–G12 events | Markdown |
| **Risk analysis** | Escalations, blocker history, security mediums, assumption log | Sprint close + any escalation | Ranked register (likelihood × impact, owner, mitigation) |
| **Technical debt report** | `APPROVED_WITH_COMMENTS` items, deferred findings, coverage deltas, TODO-equivalent debt tickets | Sprint close | Ranked ledger with paydown tasks |

**TITAN-WFL-020 (MUST) — Derived, versioned artifacts.** All six artifacts SHALL regenerate automatically from the event log on their trigger conditions, be versioned per regeneration, and be exportable (Markdown/JSON) via API. *Acceptance:* deleting all artifact rows and replaying the event log reproduces byte-identical current versions.

**TITAN-WFL-021 (MUST) — Debt is tracked, never hidden.** Any deviation from the constitution accepted at review (6.6) SHALL create a technical-debt ticket with a paydown task proposed into a future sprint; the debt ledger total SHALL appear on the project dashboard. *Acceptance:* every `APPROVED_WITH_COMMENTS` verdict has a corresponding open or closed debt ticket.

---

## 6. The Engineering Standards Constitution

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

**TITAN-WFL-034 (MUST) — Documentation as a gated deliverable.** Every project SHALL ship: README with a copy-paste Docker Compose quickstart verified by execution (G11); architecture guide generated from the ArchitecturePackage; API reference from OpenAPI with prose descriptions on every endpoint, parameter, and error `type`; user manual keyed to the ProductBacklog journeys; deployment runbook (including rollback); contribution guide; troubleshooting guide seeded from defects encountered during the build; ADR index. Code-level: public functions/classes carry docstrings (Google style) / JSDoc; modules carry purpose headers; complex algorithms carry rationale comments — comments explain *why*, never narrate *what*. *Acceptance:* the G11 coverage checker reports 100% on endpoints and ADR indexing, and the quickstart executes cleanly in CI.

---

## 7. Decision Records

### ADR-WFL-01 — Stage-gated pipeline over free-form agent swarm

- **Context:** Multi-agent systems can be orchestrated as an open "swarm" (agents self-select work, converse freely) or as a gated pipeline with typed handoffs.
- **Decision:** Gated pipeline with the twelve handoff contracts of Section 2.3; agent conversation is allowed *within* a stage but stage transitions require artifacts passing gates.
- **Alternatives considered:** (a) Free-form swarm with a coordinator — rejected: non-reproducible, unauditable, unbounded token spend; (b) fully static waterfall without loops — rejected: forfeits the self-improvement requirement.
- **Trade-offs:** Gates add latency and orchestration code; in exchange we get resumability, auditability (TITAN-WFL-004), bounded cost, and testable prompt regression per stage. For an open-source, local-first platform, reproducibility outweighs raw speed.

### ADR-WFL-02 — Loop iteration cap of 3 with convergence early-exit

- **Context:** "Never accept the first draft" conflicts with bounded cost on a laptop-class local deployment (Ollama models included).
- **Decision:** Maximum 3 full loop cycles per task, early exit on two consecutive zero-finding cycles, oscillation short-circuit to Architect arbitration (TITAN-WFL-013/014).
- **Alternatives considered:** (a) Unbounded until reviewer approval — rejected: livelock risk, unbounded spend; (b) single mandatory revision — rejected: benchmarking in comparable systems shows second-cycle findings are common; (c) dynamic cap by model quality — deferred to Volume 10's self-tuning.
- **Trade-offs:** Some residual defects will pass at cycle 3 and be caught at QA/Security stages instead — acceptable because macro gates backstop the micro loop; escalation ensures humans see chronic failures.

### ADR-WFL-03 — Trunk-based development with short-lived task branches

- **Context:** Agents work in parallel on a monorepo (canonical, Volume 1); merge strategy determines integration pain.
- **Decision:** Trunk-based: one short-lived branch per task (`feat/TSK-nnn-slug`), merged to `main` only via the self-improvement loop + CI; integration branch per sprint scope assembled for H6; no long-lived feature branches.
- **Alternatives considered:** (a) GitFlow — rejected: long-lived branches multiply merge conflicts between parallel agents; (b) direct-to-trunk commits — rejected: violates TITAN-WFL-010 (no unreviewed drafts).
- **Trade-offs:** Requires disciplined task decomposition (≤1 agent-day, TITAN-WFL @ H4) and feature flags for partially complete features (mechanics in Volume 9); in exchange, merge conflicts stay small and the dependency graph stays honest.

### ADR-WFL-04 — Standards enforced by machines first, reviewer second

- **Context:** LLM reviewers are strong at judgment, inconsistent at mechanical rule checking.
- **Decision:** Split the constitution into a machine-checkable subset (lint, import boundaries, coverage, naming, secret scan — enforced in CI before review) and a judgment subset (SOLID at design level, DDD appropriateness, duplication semantics — enforced by the Code Reviewer against the R1–R11 table).
- **Alternatives considered:** (a) Reviewer checks everything — rejected: wastes tokens on what Ruff does for free and drifts across model versions; (b) CI checks everything — rejected: architectural judgment isn't mechanically checkable.
- **Trade-offs:** Two enforcement layers to maintain; mitigated by the prompt-regression fixtures (TITAN-WFL-032) keeping the judgment layer calibrated.

---

## 8. Risks & Open Questions

| # | Risk / question | Impact | Mitigation / owner |
|---|---|---|---|
| 1 | **Reviewer–implementer collusion drift**: over time, prompt updates could make the reviewer lenient, silently degrading quality. | High | Prompt-regression fixture suite for R1–R11 runs on every prompt change; quality metrics (defects escaping to QA per 100 merges) tracked in Volume 10's evaluation harness. |
| 2 | **Gate rubric gaming**: agents may optimize for passing gate wording rather than intent (e.g., trivial tests inflating coverage). | High | Mutation-testing spot checks in QA stage (SHOULD); reviewer explicitly checks test meaningfulness (R2 covers assertion-free tests); open question: mutation testing cost on local-only hardware. |
| 3 | **Local-model capability floor**: on Ollama-class models, 3 loop cycles may not converge for complex tasks, raising escalation rates. | Medium | Task decomposition pressure at H4 (≤1 agent-day); per-provider iteration-cap configuration; escalation reports quantify the gap to inform model routing (Volume 5/10). |
| 4 | **Retry-budget interaction across stages**: a defect oscillating between QA (H7) and engineering could consume budgets in two stages and double-escalate. | Medium | Single project-level escalation queue deduplicates by defect fingerprint; cross-stage oscillation counts as one escalation. |
| 5 | **Clarification-policy tension**: 5-question cap may be too tight for genuinely ambiguous enterprise briefs. | Medium | Cap is per-project configuration; assumption logging plus H12 restatement gives the user a correction point. Open question: allow follow-up rounds after M0? Current answer: new user requests enter as backlog change requests. |
| 6 | **Artifact regeneration cost**: replaying large event logs to regenerate artifacts (TITAN-WFL-020) may get slow on big projects. | Low | Incremental materialized views with periodic snapshot compaction; replay-from-snapshot rather than genesis. |
| 7 | **Open question — human override granularity**: when a human resolves an escalation, do their edits re-enter the loop or merge directly? | Medium | Current draft position: human edits still pass CI + a single review cycle (humans are not exempt from the constitution), but this needs UX validation in Volume 4. |

---

## 9. Cross-References

- **Volume 1 — Foundation:** canonical tech-stack decisions and their justification; success metrics that gate G12 traces to.
- **Volume 2 — Multi-Agent Intelligence:** per-agent specifications (prompts, decision trees, KPIs) for every actor named in the pipeline; agent failure-recovery internals behind TITAN-WFL-019.
- **Volume 3 — Backend Engineering:** orchestrator implementation of the state machine, Redis Streams topics, Pydantic artifact schemas, WebSocket event fan-out.
- **Volume 4 — Frontend & Dashboard:** Kanban board, pipeline visualization, gate-audit trail, escalation inbox UX for Section 5 artifacts and TITAN-WFL-005 escalations.
- **Volume 5 — Memory, Knowledge & RAG:** constitution retrieval into agent context; retrospective and lessons-learned storage (TITAN-WFL-016/017).
- **Volume 7 — Integrations & Tool Ecosystem:** Git provider bindings for branch protection (TITAN-WFL-010) and PR mechanics; CI runner integration.
- **Volume 8 — Security:** the full policy behind gate G8 and the Security stage of the loop; secret-handling rules referenced in 6.4.
- **Volume 9 — Deployment & Production:** staging→production promotion, rollback mechanics behind G10, feature-flag mechanics for trunk-based development (ADR-WFL-03).
- **Volume 10 — Autonomous Evolution:** quality metrics, prompt-regression harness, and self-tuning of iteration caps referenced in ADR-WFL-02 and Risk 1.

---

*End of Volume 6 — TITAN-VOL-06, Draft v1.0.*
