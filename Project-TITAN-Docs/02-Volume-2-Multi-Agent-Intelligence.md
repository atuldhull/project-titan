# PROJECT TITAN — Autonomous Software Engineering Platform

| Field      | Value                                            |
|------------|--------------------------------------------------|
| Series     | PROJECT TITAN Specification Series (10 Volumes)  |
| Volume     | Volume 2 — Multi-Agent Intelligence (Agent Architecture) |
| Doc ID     | TITAN-VOL-02                                     |
| Status     | Draft v1.0                                       |
| Date       | 2026-07-04                                       |
| Req prefix | TITAN-AGT-NNN                                    |

**Abstract.** This volume specifies the agent organization at the heart of TITAN: sixteen role-specialized AI agents that collaborate like a real software company to turn a one-sentence client request into a production-ready application. It defines the common agent anatomy, a compact specification for every agent (purpose, responsibilities, decision authority, inputs/outputs, system-prompt design, memory access, tool access, KPIs, failure recovery), the agent hierarchy and collaboration patterns, the structured message protocol with JSON schemas for all eight message types, conflict-resolution and escalation rules, and the mechanism by which agents are instantiated as stateless LLM calls through the provider-agnostic gateway. Technology choices (Python 3.12/FastAPI orchestrator, PostgreSQL 16, Redis 7 Streams, Qdrant, provider gateway with Anthropic/OpenAI/Google/Ollama) are canonical decisions owned and justified by Volume 1; this volume builds on them without restating the rationale.

---

## Table of Contents

- 1. Introduction and Scope
- 2. Agent Model Fundamentals
  - 2.1 Anatomy of a TITAN Agent
  - 2.2 Shared Defaults (applies to every agent)
  - 2.3 Baseline Requirements
- 3. Agent Roster and Hierarchy
  - 3.1 Roster
  - 3.2 Hierarchy Diagram
- 4. Agent Specifications
  - 4.1 CEO Agent … 4.16 Critic Agent
- 5. Inter-Agent Collaboration Patterns
- 6. Structured Message Protocol
  - 6.1 Envelope
  - 6.2 Message Types and JSON Examples
  - 6.3 Persistence and Delivery
- 7. Conflict Resolution and Escalation
- 8. Agent Instantiation via the Provider Gateway
- 9. Architecture Decision Records
- 10. Risks and Open Questions
- 11. Cross-References

---

## 1. Introduction and Scope

TITAN does not build one giant AI; it builds an organization (core philosophy, Volume 1). Each agent is a role, not a process: a versioned bundle of system prompt, decision authority, memory scopes, tool grants, and KPIs, executed as stateless LLM calls. This volume is the employment contract for every role. A developer (or an AI coding agent) implementing the agent runtime, the message bus consumers, and the per-agent configuration should need only this document plus the interface contracts in Volumes 3 (backend), 5 (memory), and 7 (tools).

In scope: agent definitions, hierarchy, collaboration patterns, message protocol, conflict/escalation rules, LLM instantiation. Out of scope: the orchestration engine's task-state machine (Volume 6), memory internals (Volume 5), tool implementations (Volume 7), and the gateway's provider adapters (Volume 3).

## 2. Agent Model Fundamentals

### 2.1 Anatomy of a TITAN Agent

Every agent is defined by a declarative **Agent Definition** file (`agents/<agent_id>.yaml`, validated by a Pydantic v2 model `AgentDefinition`), containing:

1. `agent_id`, `display_name`, `tier` (0–3 or `cross`), `supervisor` (agent_id).
2. `system_prompt_template` — Jinja2 template reference; composed at call time (Section 8).
3. `authority` — lists of `decides_alone` and `must_escalate` decision classes.
4. `memory_scopes` — read/write grants against the memory system (Volume 5 scopes: `short_term`, `project`, `organization`, `code_patterns`, `architecture`, `lessons_learned`).
5. `tool_grants` — allow-list of tool IDs from the tool registry (Volume 7). Tools not listed are invisible to the agent.
6. `model_policy` — model tier (`reasoning`, `standard`, `fast`), temperature, max output tokens, structured-output schema.
7. `kpis` — metric names emitted to Prometheus via OpenTelemetry.
8. `failure_policy` — overrides to the default recovery ladder (2.2).

### 2.2 Shared Defaults (applies to every agent)

To keep the per-agent specs in Section 4 compact, the following defaults hold unless a spec states otherwise:

- **Memory:** read access to project `short_term` and `project` memory; write access to its own working notes in `short_term`; every agent MAY write `lessons_learned` entries tagged with its role.
- **Outputs:** every agent emits structured messages (Section 6) — never free prose to the bus. All substantive decisions are recorded as `DecisionLog` messages.
- **Failure recovery ladder (default):** (1) automatic retry with same context on transient LLM/tool error (max 2, exponential backoff); (2) self-critique retry — the agent's output failed schema validation or acceptance checks, so it is re-invoked with the failure appended (max 2); (3) escalate to supervisor via `ProgressReport(status="blocked")`; (4) supervisor reassigns, decomposes, or escalates to human. Every rung is logged and traced.
- **Tools:** all agents get `memory.search`, `memory.write_note`, and `messages.send`. Anything else is an explicit grant.
- **KPIs (universal):** task success rate, first-pass acceptance rate, mean tokens per completed task, mean wall-clock latency per task, escalation rate.

### 2.3 Baseline Requirements

| ID | Priority | Requirement | Acceptance Criteria |
|---|---|---|---|
| TITAN-AGT-001 | MUST | Every agent is defined by a declarative `AgentDefinition` (YAML + Pydantic) with the eight fields in 2.1. | Loading `agents/*.yaml` validates against the schema; a missing field fails CI. |
| TITAN-AGT-002 | MUST | Agents are stateless between invocations; all durable state lives in PostgreSQL/Redis/Qdrant. | Killing and restarting the agent runtime mid-project loses no accepted work; tasks resume from persisted state. |
| TITAN-AGT-003 | MUST | Agents communicate only via the structured message protocol (Section 6); no out-of-band side channels. | Code review + integration test assert every inter-agent interaction produces a persisted `agent_messages` row. |
| TITAN-AGT-004 | MUST | Every agent enforces its `tool_grants` allow-list at the runtime layer, not only in the prompt. | A prompt-injected request for an ungranted tool is rejected by the runtime and logged as a security event. |
| TITAN-AGT-005 | MUST | The default failure-recovery ladder (2.2) is implemented in the shared runtime, configurable per agent. | Fault-injection tests demonstrate all four rungs, with OpenTelemetry spans for each. |
| TITAN-AGT-006 | SHOULD | Adding a new agent requires only a new YAML definition + prompt template, no core-code change. | A sample "Localization Agent" can be added in a test with zero orchestrator diffs. |
| TITAN-AGT-007 | MUST | The full roster runs on a single laptop under Docker Compose using local models via Ollama. | `docker compose up` + Ollama profile completes the reference "todo app" project end-to-end offline. |

## 3. Agent Roster and Hierarchy

### 3.1 Roster

Sixteen agents. Fourteen come directly from the master prompt; two are additions justified below.

| # | Agent | agent_id | Tier | Supervisor |
|---|---|---|---|---|
| 1 | CEO | `ceo` | 0 | Human client |
| 2 | Orchestrator/Planner *(addition)* | `orchestrator` | 1 | `ceo` |
| 3 | Product Manager | `pm_product` | 1 | `ceo` |
| 4 | System Architect | `architect` | 1 | `ceo` |
| 5 | Project Manager | `pm_project` | 2 | `orchestrator` |
| 6 | Backend Engineer | `eng_backend` | 3 | `pm_project` |
| 7 | Frontend Engineer | `eng_frontend` | 3 | `pm_project` |
| 8 | Database Engineer | `eng_database` | 3 | `pm_project` |
| 9 | DevOps | `eng_devops` | 3 | `pm_project` |
| 10 | QA | `qa` | 3 | `pm_project` |
| 11 | Security | `security` | 3 (veto powers) | `pm_project` |
| 12 | Code Reviewer | `reviewer` | cross | `architect` |
| 13 | Documentation | `docs` | 3 | `pm_project` |
| 14 | Research | `research` | cross | `orchestrator` |
| 15 | Memory | `memory` | cross | `orchestrator` |
| 16 | Critic *(addition)* | `critic` | cross | `architect` |

**Justification for additions.** The **Orchestrator/Planner** is named explicitly in the source workflow ("User Request → CEO Agent → Planner → …"): someone must own plan decomposition, message routing, scheduling, and deadlock detection — none of the fourteen base roles does. The **Critic** exists because the mandated self-improvement loop (Implementation → Review → **Critique** → Refactor → …) requires adversarial critique of *non-code* artifacts (plans, architectures, specs, docs) that the Code Reviewer (scoped to pull requests) does not cover. No third agent is added: release management is split between CEO (approval) and DevOps (execution), and UX is owned by the Frontend Engineer.

TITAN-AGT-008 (MUST): The platform ships with exactly this roster enabled by default; roster changes are configuration (2.3/TITAN-AGT-006). *Acceptance:* fresh install lists these 16 agents in the dashboard Agent Status view.

### 3.2 Hierarchy Diagram

```
                                ┌─────────────────┐
                                │   HUMAN CLIENT   │
                                └────────┬────────┘
                          requirements,  │  approvals, feedback
                                ┌────────▼────────┐
   TIER 0 — Vision & Release    │    CEO AGENT     │
                                └────────┬────────┘
              ┌──────────────────────────┼───────────────────────────┐
      ┌───────▼────────┐        ┌────────▼────────┐         ┌────────▼────────┐
      │ PRODUCT MANAGER │◄──────►│  ORCHESTRATOR / │◄───────►│ SYSTEM ARCHITECT │
      │ (what to build) │        │  PLANNER (how/  │         │ (how it's shaped)│
      └────────────────┘        │  when/by whom)  │         └────────┬────────┘
   TIER 1 — Planning & Design   └────────┬────────┘                  │ arbitration,
                                         │                           │ design review
                                ┌────────▼────────┐                  │
   TIER 2 — Coordination        │ PROJECT MANAGER  │◄─────────────────┘
                                └────────┬────────┘
        ┌──────────┬──────────┬─────────┼──────────┬──────────┬──────────┐
   ┌────▼───┐ ┌────▼────┐ ┌───▼────┐ ┌──▼───┐ ┌────▼───┐ ┌────▼────┐ ┌───▼──┐
   │BACKEND │ │FRONTEND │ │DATABASE│ │DEVOPS│ │   QA   │ │SECURITY │ │ DOCS │
   │ENGINEER│ │ENGINEER │ │ENGINEER│ │      │ │        │ │ (veto)  │ │      │
   └────────┘ └─────────┘ └────────┘ └──────┘ └────────┘ └─────────┘ └──────┘
   TIER 3 — Builders & Gatekeepers

   CROSS-CUTTING (serve all tiers, report as shown in 3.1):
   ┌───────────────┐ ┌───────────┐ ┌────────────┐ ┌──────────┐
   │ CODE REVIEWER │ │ RESEARCH  │ │   MEMORY   │ │  CRITIC  │
   │ gates every PR│ │ on demand │ │ curates KB │ │ audits   │
   └───────────────┘ └───────────┘ └────────────┘ └──────────┘
```

## 4. Agent Specifications

Each spec lists only deltas from the shared defaults in 2.2. "Escalates" always means via the messages and rules of Sections 6–7.

### 4.1 CEO Agent (`ceo`)

- **Purpose:** Translate the client's intent into product vision; own scope, priority, and release approval.
- **Responsibilities:** Interpret the client request; ask only essential clarification questions (hard cap: one clarification round unless the client volunteers more); define vision and success criteria; prioritize features; approve/reject releases; deliver client-facing status summaries.
- **Decides alone:** Feature prioritization, scope cuts within stated budget, release go/no-go, accepting PM roadmaps.
- **Must escalate (to human client):** Budget/cost-ceiling changes, material scope expansion, legal/compliance-sensitive features, security-veto overrides (7.4), project cancellation.
- **Inputs → Outputs:** Client request, `MeetingSummary` digests, release-candidate reports → vision document (project memory), prioritized feature list, `TaskAssignment` to `pm_product`/`orchestrator`, `DecisionLog` entries, release approvals.
- **System-prompt outline:** identity ("chief executive of an autonomous software company"); prime directive (client value over technical elegance); clarification-question budget; output schemas; explicit list of human-escalation triggers; tone rules for client communication.
- **Memory:** +write `organization` (vision, client preferences); read `lessons_learned` (all roles).
- **Tools:** `client.message` (the only agent allowed to contact the human), `reports.generate`.
- **KPIs:** client clarification rounds per project (target ≤ 1), release-approval reversal rate, client-satisfaction score, vision-drift incidents.
- **Failure recovery:** No supervisor agent — rung 3 of the ladder goes directly to the human client with a structured options memo.

### 4.2 Orchestrator/Planner Agent (`orchestrator`)

- **Purpose:** Convert the approved vision into an executable plan; route work; keep the organization unblocked.
- **Responsibilities:** Milestone/phase decomposition; dependency-graph construction; agent scheduling and parallelization; watchdog for stalled tasks and message deadlocks; convening ConflictResolution threads; maintaining the run-loop cadence (Volume 6 owns the state machine; the Orchestrator operates it).
- **Decides alone:** Task ordering, parallel-vs-serial execution, retry/reassign of blocked tasks, invoking Research or Critic.
- **Must escalate:** Milestone changes (→ CEO), architectural sequencing disputes (→ Architect), resource exhaustion such as token-budget overrun (→ CEO).
- **Inputs → Outputs:** Vision doc, roadmap, architecture plan, all `ProgressReport`s → dependency graph (PostgreSQL), `TaskAssignment`s to `pm_project`, `MeetingSummary` messages, deadlock alerts.
- **System-prompt outline:** identity (chief of staff/program director); planning heuristics (small tasks, ≤ 1 owner each, explicit dependencies, acceptance criteria mandatory); watchdog rules with concrete timeouts; strict JSON plan schema.
- **Memory:** +read/write `project` plan sections; read `architecture`.
- **Tools:** `tasks.create/update/graph`, `scheduler.enqueue`, `watchdog.configure`.
- **KPIs:** plan-rework rate, mean task idle time, deadlocks detected/resolved, critical-path accuracy.
- **Failure recovery:** If the Orchestrator itself stalls, a runtime-level (non-LLM) watchdog restarts it from persisted plan state — TITAN-AGT-009 (MUST): the Orchestrator watchdog is implemented in the FastAPI runtime, not as another LLM agent. *Acceptance:* kill the orchestrator consumer mid-plan; a replacement resumes within 30 s with no duplicate task assignments.

### 4.3 Product Manager Agent (`pm_product`)

- **Purpose:** Turn vision into user stories, acceptance criteria, and a sprint-ready roadmap.
- **Responsibilities:** User stories (INVEST format), acceptance criteria per story, roadmap and release plan, sprint goals, backlog grooming, persona notes.
- **Decides alone:** Story slicing, acceptance-criteria wording, backlog ordering within CEO priorities.
- **Must escalate:** New scope discovered during grooming (→ CEO), technically infeasible stories flagged by Architect (→ ConflictResolution).
- **Inputs → Outputs:** Vision doc, Research findings, QA feedback → story backlog (PostgreSQL), roadmap, `ReviewRequest` to CEO for roadmap sign-off.
- **System-prompt outline:** identity; INVEST + Gherkin-style acceptance-criteria templates; "no story without testable criteria" rule; anti-gold-plating instruction.
- **Memory:** +write `project` (backlog, roadmap); read `organization` (client preferences).
- **Tools:** `tasks.create` (story type), `roadmap.render`.
- **KPIs:** story rejection rate by engineers (ambiguity), criteria coverage (% stories with testable criteria = 100%), mid-sprint scope-change count.
- **Failure recovery:** Default ladder; supervisor CEO.

### 4.4 System Architect Agent (`architect`)

- **Purpose:** Own technical design, technology fit, and cross-cutting consistency.
- **Responsibilities:** System architecture within the canonical stack (Volume 1); component boundaries and API contracts; data-flow and integration design; scalability review; ADR authorship; arbitration of technical disputes (7.2); final design sign-off on ArchitectureDiscussion threads.
- **Decides alone:** Internal component design, patterns (repository, CQRS where justified), library selection within the canonical stack, ADR acceptance.
- **Must escalate:** Any deviation from the canonical stack (→ CEO + human), cost-bearing infrastructure choices (→ CEO), security-relevant design (co-sign with Security).
- **Inputs → Outputs:** Roadmap, stories, Research reports → architecture document, ADRs (`DecisionLog`), API contract specs, `ArchitectureDiscussion` replies, arbitration verdicts.
- **System-prompt outline:** identity (principal architect); canonical-stack constraints stated verbatim; design heuristics (boring technology, explicit contracts, evolutionary architecture); ADR output schema; arbitration protocol.
- **Memory:** +read/write `architecture`; read `code_patterns`, `lessons_learned` (all).
- **Tools:** `repo.read`, `diagrams.render_ascii`, `adr.create`.
- **KPIs:** ADR count vs. undocumented decisions (target: zero undocumented), post-design rework rate, arbitration turnaround time.
- **Failure recovery:** Default ladder; supervisor CEO.

### 4.5 Project Manager Agent (`pm_project`)

- **Purpose:** Day-to-day execution: tasks, assignments, progress, blockers.
- **Responsibilities:** Break stories into engineering tasks; assign to Tier-3 agents by skill and load; track progress and estimates; detect blockers early; maintain Kanban state; reopen tasks that fail review/QA; produce sprint `MeetingSummary`.
- **Decides alone:** Task assignment and reassignment, estimates, task reopening, intra-sprint reordering.
- **Must escalate:** Sprint-goal risk (→ Orchestrator), repeated failure by one agent on one task (≥ 3 cycles → Orchestrator, rule 7.5), inter-agent conflicts it cannot settle in one round.
- **Inputs → Outputs:** Stories, dependency graph, `ProgressReport`s → `TaskAssignment`s, Kanban updates, blocker alerts, sprint summaries.
- **System-prompt outline:** identity (delivery-focused engineering manager); assignment heuristics (single owner, matching tool grants, load ≤ 2 active tasks/agent); blocker-detection rules; status-report schema.
- **Memory:** +write `project` (task state mirrors).
- **Tools:** `tasks.*` (full CRUD), `kanban.update`, `metrics.read`.
- **KPIs:** on-time task completion rate, blocker mean-time-to-detection, reopen rate, agent utilization balance.
- **Failure recovery:** Default ladder; supervisor Orchestrator.

### 4.6 Backend Engineer Agent (`eng_backend`)

- **Purpose:** Implement server-side functionality for generated projects.
- **Responsibilities:** APIs (REST/WebSocket), authentication/authorization wiring, business logic, service-layer performance, unit tests for own code, PR creation.
- **Decides alone:** Implementation details within the API contract, internal refactors, library helpers already approved in `code_patterns`.
- **Must escalate:** Contract changes (→ Architect via `ArchitectureDiscussion`), new dependencies (→ Architect + Security), performance targets unmet after 2 attempts (→ PM).
- **Inputs → Outputs:** `TaskAssignment` with story + contract + acceptance criteria → code commits/PR, unit tests, `ReviewRequest` to `reviewer`, `ProgressReport`s, `BugReport`s for defects found in others' code.
- **System-prompt outline:** identity (senior backend engineer); engineering standards summary from Volume 8 (SOLID, clean architecture, no TODOs/placeholders); output contract (diff + tests + self-review checklist); tool-use protocol.
- **Memory:** +read `code_patterns`, `architecture`; write `code_patterns` proposals (curated by Memory Agent).
- **Tools:** `repo.read/write/branch`, `terminal.run` (sandboxed), `tests.run`, `research.request`.
- **KPIs:** first-pass review acceptance rate, test coverage of own diffs (≥ 80%), defect-escape rate to QA, mean cycles per task.
- **Failure recovery:** Default ladder; rung 2 self-critique explicitly includes failing test output and reviewer comments.

### 4.7 Frontend Engineer Agent (`eng_frontend`)

- **Purpose:** Implement UI/UX for generated projects.
- **Responsibilities:** UI components and pages, UX flows, responsive design, accessibility (WCAG 2.1 AA), client state management, frontend tests.
- **Decides alone:** Component structure, styling within the design language, state-management wiring within Architect-approved patterns.
- **Must escalate:** API-contract gaps (→ Backend via `ArchitectureDiscussion`), design-language changes (→ Architect), accessibility conflicts with requested features (→ PM Product).
- **Inputs → Outputs:** Stories, API contracts, design tokens → components, pages, tests, `ReviewRequest`s, `ProgressReport`s.
- **System-prompt outline:** identity (senior frontend engineer); canonical frontend stack constraints; accessibility checklist embedded; visual/UX heuristics; output contract (diff + tests + Storybook-style usage notes where applicable).
- **Memory/Tools/KPIs/Recovery:** As Backend (4.6), with KPI addition: accessibility-check pass rate.

### 4.8 Database Engineer Agent (`eng_database`)

- **Purpose:** Own data modeling and data-layer performance for generated projects.
- **Responsibilities:** Schema design, migrations (forward + rollback), indexing, query optimization, data-integrity constraints, seed/fixture data.
- **Decides alone:** Index and constraint choices, migration mechanics, query rewrites.
- **Must escalate:** Schema changes that break API contracts (→ Architect), destructive migrations (→ PM + DevOps co-sign), storage-engine choices beyond the canonical stack (→ Architect).
- **Inputs → Outputs:** Domain model from Architect, slow-query reports → DDL/migrations, ERD (ASCII + machine-readable), optimization PRs, `ReviewRequest`s.
- **System-prompt outline:** identity (database specialist); normalization-first-then-justify-denormalization heuristic; migration safety rules (always reversible, never lossy without co-sign); output contract.
- **Memory:** +read/write `architecture` (data sections), `code_patterns` (query patterns).
- **Tools:** `repo.*`, `db.execute` (sandboxed dev DB only), `db.explain`, `tests.run`.
- **KPIs:** migration failure rate (target 0), slow-query count post-optimization, schema-rework rate.
- **Failure recovery:** Default ladder; destructive-operation rung escalates immediately (skips retries).

### 4.9 DevOps Agent (`eng_devops`)

- **Purpose:** Make everything buildable, deployable, and observable.
- **Responsibilities:** Dockerfiles and Docker Compose for local-first dev; Kubernetes manifests for production; CI/CD pipelines; infrastructure-as-code; monitoring/logging/alerting wiring (OpenTelemetry, Prometheus, Grafana, Loki); environment configuration and secret plumbing (values owned by Security).
- **Decides alone:** Pipeline structure, image optimization, alert thresholds within SLOs.
- **Must escalate:** Production topology changes (→ Architect), anything cost-bearing (→ CEO), secret-handling design (→ Security co-sign).
- **Inputs → Outputs:** Repo state, architecture doc → CI/CD configs, container/K8s manifests, dashboards, deployment `ProgressReport`s, release execution after CEO approval.
- **System-prompt outline:** identity (platform/SRE engineer); local-first constraint stated as hard rule ("every artifact must run under Docker Compose on one laptop"); pipeline design heuristics; rollback-first deployment doctrine.
- **Memory:** +read `architecture`; write `code_patterns` (infra patterns).
- **Tools:** `repo.*`, `terminal.run` (sandboxed), `docker.*`, `k8s.apply` (env-gated), `ci.trigger`, `observability.query`.
- **KPIs:** pipeline success rate, mean deploy time, rollback success rate (target 100%), local-compose bootstrap success.
- **Failure recovery:** Default ladder; failed deploys auto-rollback before any escalation.

### 4.10 QA Agent (`qa`)

- **Purpose:** Independently verify that built software meets acceptance criteria.
- **Responsibilities:** Test plans per story; unit-test gap analysis; integration and end-to-end tests; regression suite curation; performance smoke tests; `BugReport` filing with reproduction steps.
- **Decides alone:** Test design, pass/fail verdicts against written acceptance criteria, regression-suite membership.
- **Must escalate:** Ambiguous acceptance criteria (→ PM Product), release-blocking defect disputes (→ ConflictResolution with severity evidence).
- **Inputs → Outputs:** Stories + criteria, candidate builds → test code, test-run reports, `BugReport`s, quality-gate verdicts on `ReviewRequest`s.
- **System-prompt outline:** identity (adversarial QA engineer — "your job is to break it"); verdicts must cite specific acceptance criteria; reproduction-steps schema; flaky-test triage rules.
- **Memory:** +read `project` (criteria), `lessons_learned` (defect patterns); write defect-pattern lessons.
- **Tools:** `repo.read/write` (test dirs), `tests.run`, `browser.automation` (E2E), `terminal.run` (sandboxed).
- **KPIs:** defect-escape rate to "production" (post-approval), test flakiness rate, criteria coverage per story, mean bug-report actionability (fixed without clarification).
- **Failure recovery:** Default ladder; a QA verdict is never auto-overridden by retries — disputes go to Section 7.

### 4.11 Security Agent (`security`)

- **Purpose:** Enforce security posture across all generated artifacts and the platform's own workflows.
- **Responsibilities:** Threat modeling per feature, dependency and secret scanning, OWASP-aligned audits, security test cases, secret-management design review, RBAC review of generated apps, audit-log completeness checks.
- **Decides alone:** Vulnerability severity ratings; **security veto** — blocking any merge/release with a Critical/High finding (see 7.4).
- **Must escalate:** Veto overrides (only human client can override, via CEO), novel threat classes needing research (→ Research), compliance interpretations (→ CEO → human).
- **Inputs → Outputs:** Diffs, dependency manifests, architecture docs → findings reports (`BugReport` with `security=true`), threat models, veto verdicts on `ReviewRequest`s, `DecisionLog` for accepted risks.
- **System-prompt outline:** identity (application security engineer); OWASP Top 10 + secret-handling checklist embedded; severity rubric (CVSS-like); "no silent risk acceptance" rule — every accepted risk becomes a DecisionLog entry.
- **Memory:** +read everything project-scoped; write `lessons_learned` (vulnerability patterns).
- **Tools:** `repo.read`, `scanner.dependencies`, `scanner.secrets`, `scanner.sast`, `tests.run`.
- **KPIs:** vulnerabilities caught pre-merge vs. post-merge, false-positive rate, mean time to security verdict, unresolved-finding age.
- **Failure recovery:** Default ladder; if the Security agent is unavailable, merges requiring its sign-off queue rather than proceed (fail-closed). TITAN-AGT-010 (MUST): security review is fail-closed. *Acceptance:* disabling the security agent blocks (not bypasses) merge of a PR touching auth code.

### 4.12 Code Reviewer Agent (`reviewer`)

- **Purpose:** Gate every pull request; never accept the first draft on faith.
- **Responsibilities:** Review every PR for correctness, standards compliance (Volume 8), maintainability, and test adequacy; request changes with concrete, actionable comments; approve or reject; suggest simplifications.
- **Decides alone:** Approve/request-changes verdicts; style and standards rulings (Volume 8 is binding precedent).
- **Must escalate:** Architectural concerns discovered in review (→ Architect), author disputes after 2 review cycles (→ 7.2 arbitration).
- **Inputs → Outputs:** `ReviewRequest` + diff + tests + task context → review verdict, line-anchored comments, `ProgressReport` to PM on review throughput.
- **System-prompt outline:** identity (staff engineer reviewer); review rubric ordered by severity (correctness > security-adjacent > design > tests > style); "must cite the standard" rule for rejections; comment-format schema; anti-rubber-stamp instruction (must list at least what was checked).
- **Memory:** +read `code_patterns`, `architecture`, Volume 8 standards (knowledge base).
- **Tools:** `repo.read`, `tests.run`, `diff.annotate`.
- **KPIs:** post-approval defect rate, review turnaround time, comment actionability rate, rubber-stamp rate (approvals with zero substantive checks — target 0).
- **Failure recovery:** Default ladder; reviews are fail-closed like security (no auto-approve on outage).

### 4.13 Documentation Agent (`docs`)

- **Purpose:** Keep generated projects and TITAN itself documented, continuously.
- **Responsibilities:** README, API documentation (from contracts + code), architecture guides (from ADRs), user manuals, developer/contribution guides, troubleshooting guides, changelog maintenance.
- **Decides alone:** Document structure, wording, regeneration scheduling.
- **Must escalate:** Contradictions between code and ADRs (→ Architect as `BugReport`), missing information it cannot derive (→ owning agent via `ReviewRequest`).
- **Inputs → Outputs:** Merged code, ADRs, API contracts, release notes → documentation PRs (reviewed like code), doc-coverage reports.
- **System-prompt outline:** identity (technical writer-engineer); "docs are derived from artifacts, never invented" rule; audience-first templates per doc type; staleness-detection heuristics.
- **Memory:** +read all project scopes; write `project` doc index.
- **Tools:** `repo.read/write` (docs dirs), `openapi.render`, `diagrams.render_ascii`.
- **KPIs:** doc coverage (public APIs documented ≥ 95%), staleness incidents, doc-related support questions from client.
- **Failure recovery:** Default ladder; documentation failures never block merges (fail-open, flagged).

### 4.14 Research Agent (`research`)

- **Purpose:** Provide grounded technical knowledge on demand so other agents don't guess.
- **Responsibilities:** Look up library/API documentation, compare technical options with citations, summarize into reusable briefs, track ecosystem changes relevant to active projects.
- **Decides alone:** Source selection, brief structure, confidence ratings.
- **Must escalate:** Findings that contradict current architecture (→ Architect), paywalled/unavailable sources (→ requester with stated confidence downgrade).
- **Inputs → Outputs:** Research requests (structured question + context + deadline) → research briefs (question, findings, citations, confidence, recommendation), stored in the knowledge base.
- **System-prompt outline:** identity (research librarian-engineer); "cite or mark as inference" rule; brief schema; recency-checking instructions; anti-hallucination guard (unknown → say unknown).
- **Memory:** +write knowledge base (`organization` scope, curated by Memory Agent).
- **Tools:** `web.search`, `web.fetch`, `repo.read`, `memory.search`.
- **KPIs:** brief citation rate (100%), requester-usefulness rating, brief reuse count, turnaround time.
- **Failure recovery:** Default ladder; on web-tool outage, answers from knowledge base with explicit "offline, possibly stale" flag (supports the offline laptop mode of TITAN-AGT-007).

### 4.15 Memory Agent (`memory`)

- **Purpose:** Curate the organization's long-term memory so it stays useful, deduplicated, and retrievable (memory internals: Volume 5).
- **Responsibilities:** Promote/demote memories between scopes; deduplicate and merge `lessons_learned`; curate `code_patterns` (accept/reject engineer proposals); maintain embeddings freshness in Qdrant; run pruning/compression jobs; answer complex retrieval requests other agents' `memory.search` can't satisfy.
- **Decides alone:** Curation verdicts, pruning within retention policy, embedding re-index scheduling.
- **Must escalate:** Retention-policy changes (→ CEO), deleting decision history (never allowed — flag instead; DecisionLogs are append-only).
- **Inputs → Outputs:** Memory-write proposals, retrieval requests, project post-mortems → curated memory entries, retrieval result sets, memory-health reports.
- **System-prompt outline:** identity (knowledge curator); curation rubric (generality, evidence, non-duplication); "decision history is immutable" rule; retrieval-answer schema with provenance.
- **Memory:** read/write ALL scopes (unique grant).
- **Tools:** `memory.*` (full suite incl. `promote`, `prune`, `reindex`), `qdrant.admin`.
- **KPIs:** retrieval precision on eval set, duplicate rate in knowledge base, memory-driven rework savings (lessons reused per project).
- **Failure recovery:** Default ladder; pruning jobs are transactional and reversible for 30 days.

### 4.16 Critic Agent (`critic`)

- **Purpose:** Adversarial second opinion on non-code artifacts, powering the mandated critique step of the self-improvement loop.
- **Responsibilities:** Critique plans, architectures, specs, test plans, and docs before they are accepted; red-team assumptions; identify missing failure modes; score artifacts against their own stated goals.
- **Decides alone:** Critique content and severity labels (advisory only — the Critic never blocks; owners and gatekeepers decide).
- **Must escalate:** Nothing; it is terminal-advisory. Ignored Critical-severity critiques are auto-logged to the owning agent's supervisor.
- **Inputs → Outputs:** Artifact + goals → structured critique (strengths, weaknesses ranked by severity, concrete revisions), attached to the artifact's thread.
- **System-prompt outline:** identity (professional skeptic, steelman-then-attack); severity rubric; "every weakness needs a concrete fix suggestion" rule; instructed to run on a different model tier than the artifact's author when configured (multi-model critique, Volume 10 hook).
- **Memory:** +read `lessons_learned` (all), `architecture`.
- **Tools:** `repo.read`, `memory.search` only (deliberately minimal).
- **KPIs:** critique adoption rate, post-critique defect reduction, false-alarm rate.
- **Failure recovery:** Default ladder; Critic outage never blocks the pipeline (fail-open, logged).

TITAN-AGT-011 (MUST): Each agent's `decides_alone` / `must_escalate` lists above are encoded in its AgentDefinition and enforced by the runtime where mechanically checkable (vetoes, co-signs, fail-open/closed behavior). *Acceptance:* contract tests per agent verify at least the veto, co-sign, and fail-open/closed rules.

## 5. Inter-Agent Collaboration Patterns

TITAN standardizes five collaboration patterns; every workflow in Volume 6 composes them.

1. **Delegation chain** (Tier 0→1→2→3): work flows down as `TaskAssignment`, status flows up as `ProgressReport`. Every assignment has exactly one owner and written acceptance criteria.
2. **Review gate:** producer sends `ReviewRequest`; gatekeeper (Reviewer, QA, Security, Architect, or CEO per artifact type) returns a verdict. Gates are fail-closed for code/security, fail-open for docs/critique. The self-improvement loop is this pattern applied in sequence: Implementation → Review (`reviewer`) → Critique (`critic`) → Refactor (owner) → Testing (`qa`) → Security scan (`security`) → Approval (CEO for releases) → Merge (DevOps).
3. **Consultation:** any agent may request Research or Memory retrieval mid-task without escalation; consultations are messages, so they are persisted and traceable.
4. **Discussion thread:** design questions run as `ArchitectureDiscussion` threads with a named moderator (Architect) and a decision deadline; outcome is always a `DecisionLog`.
5. **Ceremony:** Orchestrator-convened structured meetings (sprint planning, post-mortem) — a bounded round of messages from named participants, summarized as `MeetingSummary` and stored.

| ID | Priority | Requirement | Acceptance Criteria |
|---|---|---|---|
| TITAN-AGT-012 | MUST | Every task has exactly one owner agent at any time. | DB constraint + test: concurrent assignment attempts fail. |
| TITAN-AGT-013 | MUST | The full self-improvement loop runs on every merged code artifact. | Pipeline trace for any merged PR shows all seven stages with verdicts. |
| TITAN-AGT-014 | SHOULD | Discussion threads auto-close with a DecisionLog within their deadline or escalate. | Watchdog test: an idle thread past deadline triggers escalation to the moderator's supervisor. |

## 6. Structured Message Protocol

### 6.1 Envelope

Every message shares one envelope (Pydantic model `AgentMessage`), serialized as JSON:

```json
{
  "message_id": "uuid-v7",
  "schema_version": "1.0",
  "type": "TaskAssignment",
  "project_id": "uuid",
  "task_id": "uuid | null",
  "thread_id": "uuid | null",
  "correlation_id": "uuid (request/response pairing)",
  "from_agent": "pm_project",
  "to_agent": "eng_backend",
  "priority": "critical | high | normal | low",
  "requires_ack": true,
  "created_at": "2026-07-04T10:15:00Z",
  "payload": { }
}
```

`payload` is discriminated by `type`. Unknown `type` values are rejected; unknown payload fields are rejected (strict Pydantic).

### 6.2 Message Types and JSON Examples

Eight types are normative (TITAN-AGT-015, MUST — the bus accepts only these; *acceptance:* posting a ninth type returns a validation error and is logged).

**TaskAssignment** — creates/assigns work; the only way work changes hands.

```json
{ "type": "TaskAssignment", "payload": {
  "title": "Implement POST /api/v1/orders",
  "description": "Create order endpoint per contract C-114.",
  "acceptance_criteria": ["201 + body per contract", "401 unauthenticated", "unit tests >= 80% diff coverage"],
  "story_id": "uuid", "depends_on": ["task-uuid-1"],
  "estimate_points": 3, "deadline": "2026-07-05T12:00:00Z" } }
```

**ProgressReport** — status heartbeat; also carries blockers.

```json
{ "type": "ProgressReport", "payload": {
  "status": "in_progress | blocked | completed | failed",
  "percent_complete": 60,
  "summary": "Endpoint done; writing tests.",
  "blockers": [{ "description": "Contract C-114 missing error codes", "needs": "architect" }],
  "artifacts": ["branch:feat/orders-endpoint"] } }
```

**ReviewRequest** — asks a gatekeeper for a verdict; response reuses `correlation_id`.

```json
{ "type": "ReviewRequest", "payload": {
  "artifact_type": "pull_request | plan | architecture | test_plan | release",
  "artifact_ref": "pr:42", "context_task_id": "uuid",
  "checklist": ["correctness", "tests", "standards"],
  "verdict": null } }
```

Gatekeeper reply: same payload with `"verdict": { "decision": "approve | request_changes | reject | veto", "comments": [{ "location": "src/orders.py:88", "severity": "major", "comment": "...", "standard_ref": "VOL8-ERR-004" }] }`.

**ArchitectureDiscussion** — threaded design debate.

```json
{ "type": "ArchitectureDiscussion", "payload": {
  "thread_id": "uuid", "question": "Sync or async order-payment integration?",
  "position": "Async via Redis Streams outbox.",
  "arguments": ["decouples payment latency", "replayable"],
  "responding_to": "message-uuid | null",
  "decision_deadline": "2026-07-05T09:00:00Z" } }
```

**BugReport** — defect with mandatory reproduction.

```json
{ "type": "BugReport", "payload": {
  "severity": "critical | high | medium | low",
  "security": false,
  "summary": "Order total ignores discounts",
  "reproduction_steps": ["POST /orders with coupon X", "observe total"],
  "expected": "total = 90.00", "actual": "total = 100.00",
  "suspected_component": "pricing service", "evidence": ["test:test_discount_applied FAILED"] } }
```

**DecisionLog** — append-only record of every substantive decision.

```json
{ "type": "DecisionLog", "payload": {
  "decision": "Use cursor pagination for order listing",
  "context": "Offset pagination degrades past 100k rows.",
  "alternatives": [{ "option": "offset", "rejected_because": "O(n) skip cost" }],
  "trade_offs": "No random page access.",
  "authority": "decides_alone", "reversible": true } }
```

**MeetingSummary** — outcome of a ceremony.

```json
{ "type": "MeetingSummary", "payload": {
  "ceremony": "sprint_planning | standup | post_mortem | conflict_resolution",
  "participants": ["ceo", "pm_product", "architect"],
  "decisions": ["message-uuid-of-DecisionLog"],
  "action_items": [{ "owner": "pm_project", "action": "cut story S-9", "due": "2026-07-05" }],
  "summary": "Sprint 3 scoped to checkout flow." } }
```

**ConflictResolution** — opens/advances a dispute (Section 7).

```json
{ "type": "ConflictResolution", "payload": {
  "conflict_id": "uuid",
  "parties": ["eng_backend", "qa"],
  "subject": "Is BUG-77 release-blocking?",
  "positions": [{ "agent": "qa", "position": "blocking", "evidence": ["fails AC-3 of story S-4"] }],
  "round": 1, "arbiter": null, "resolution": null } }
```

### 6.3 Persistence and Delivery

- **Transport:** Redis Streams — one inbox stream per agent (`titan:{project_id}:inbox:{agent_id}`) with a consumer group per agent runtime; NATS JetStream is the documented scale-out swap (Volume 3).
- **Persistence:** every message is written to PostgreSQL `agent_messages` (envelope columns + `payload JSONB`) *before* enqueue (outbox pattern). Nothing is lost: TITAN-AGT-016 (MUST) — *acceptance:* chaos test kills Redis mid-conversation; on recovery, no persisted message is missing from replay and no accepted message was only in Redis.
- **Delivery semantics:** at-least-once with idempotent consumers keyed on `message_id`; `requires_ack` messages unacknowledged after their type-specific timeout re-enter the pending set and trip the Orchestrator watchdog.
- **Observability:** every message emits an OpenTelemetry span linked by `correlation_id`; conversation views in the dashboard (Volume 4) are rendered from `agent_messages`.

## 7. Conflict Resolution and Escalation

1. **Direct round (mandatory first step).** Parties exchange one `ConflictResolution` round each, positions with evidence. Most disputes end here.
2. **Arbitration.** No consensus after round 1 → the Orchestrator appoints the arbiter: **Architect** for technical disputes, **PM Product** for scope disputes, **PM Project** for priority/assignment disputes. The arbiter's verdict is a `DecisionLog` and is binding.
3. **Executive escalation.** Arbitration disputed, or the dispute crosses domains → CEO decides. CEO decisions are final inside the platform.
4. **Security veto.** A Security `veto` verdict blocks merge/release regardless of any other agent's approval, including the CEO's. Only the human client (via the CEO relaying a structured risk memo) can override; the override itself becomes a permanent `DecisionLog` with `authority: "human_override"`.
5. **Deadlock rule.** Any conflict reaching round 3 without resolution, or any task reopened 3 times, auto-escalates one level and is flagged in the dashboard.
6. **Human tripwires.** Automatic human escalation on: canonical-stack deviation requests, cost-ceiling breach, security-veto override requests, project-goal ambiguity the CEO scores as unresolvable, and destructive irreversible operations outside sandboxes.

| ID | Priority | Requirement | Acceptance Criteria |
|---|---|---|---|
| TITAN-AGT-017 | MUST | Conflict flow (rules 1–3) is enforced by the runtime state machine, not left to prompt compliance. | Simulated dispute test traverses direct → arbitration → executive with persisted state transitions. |
| TITAN-AGT-018 | MUST | Security veto blocks merges at the pipeline level. | A vetoed PR cannot be merged even by a CEO-approved release job. |
| TITAN-AGT-019 | MUST | Every resolution produces a DecisionLog; DecisionLogs are append-only. | UPDATE/DELETE on decision rows is rejected by DB policy. |
| TITAN-AGT-020 | SHOULD | Deadlock (rule 5) detection runs in the Orchestrator watchdog with configurable thresholds. | Threshold config change takes effect without redeploy. |

## 8. Agent Instantiation via the Provider Gateway

Agents are not resident processes with private state; an "agent" exists only during a **turn** — one hydrate→call→act cycle executed by the shared Agent Runtime (a pool of async FastAPI workers).

```
 Redis Stream (inbox)      AGENT RUNTIME (Python 3.12, async)             Provider Gateway
 ─────────────────────     ─────────────────────────────────────────      ─────────────────
  XREADGROUP ──────────►  1. Load AgentDefinition (yaml, cached)
                          2. Hydrate context:
                             • system prompt (Jinja2 template)
                             • task + thread messages (PostgreSQL)
                             • memory retrieval (Qdrant + scopes)
                             • tool schemas (granted tools only)
                          3. Call LLM ────────────────────────────────►  route by model_policy:
                                                                          anthropic | openai |
                          4. Validate structured output (Pydantic)  ◄──  google | ollama
                          5. Execute tool calls (sandboxed, audited)
                          6. Emit messages (outbox → Postgres → Redis)
                          7. XACK + OTel span + KPI metrics
```

**Prompt composition (order is normative):** (1) agent identity and mission; (2) canonical-stack and platform constraints; (3) authority section rendered from `decides_alone`/`must_escalate`; (4) role heuristics and rubrics; (5) output JSON schema; (6) retrieved memory (provenance-tagged); (7) current task/thread context. Templates are versioned in-repo; prompt changes go through the normal PR + review gates.

**Model policy** is configuration only, per the canonical multi-LLM decision:

```yaml
# config/model-policy.yaml
tiers:
  reasoning: { provider: anthropic, model: claude-opus-latest,  temperature: 0.2 }
  standard:  { provider: anthropic, model: claude-sonnet-latest, temperature: 0.3 }
  fast:      { provider: openai,    model: gpt-mini-latest,      temperature: 0.2 }
profiles:
  local-free:            # laptop / offline profile (hard requirement)
    reasoning: { provider: ollama, model: qwen2.5-coder:32b }
    standard:  { provider: ollama, model: qwen2.5-coder:14b }
    fast:      { provider: ollama, model: llama3.2:3b }
agents:
  ceo: reasoning
  architect: reasoning
  critic: reasoning
  eng_backend: standard
  eng_frontend: standard
  eng_database: standard
  eng_devops: standard
  reviewer: standard
  security: reasoning
  qa: standard
  pm_product: standard
  pm_project: fast
  orchestrator: standard
  docs: fast
  research: fast
  memory: fast
```

| ID | Priority | Requirement | Acceptance Criteria |
|---|---|---|---|
| TITAN-AGT-021 | MUST | Switching any agent's provider/model requires only editing model-policy config. | Change `architect` to `ollama` profile; next turn uses it, zero code changes, verified in traces. |
| TITAN-AGT-022 | MUST | All LLM calls go through the gateway; no agent code imports provider SDKs directly. | Static check in CI forbids provider SDK imports outside `gateway/`. |
| TITAN-AGT-023 | MUST | Structured outputs are schema-validated; invalid output triggers the self-critique retry rung. | Fuzz test with malformed outputs shows retry-then-escalate, never silent acceptance. |
| TITAN-AGT-024 | MUST | Tool execution is sandboxed and audited (who, what, args, result hash) per turn. | Audit log row exists for 100% of tool calls in an E2E run. |
| TITAN-AGT-025 | SHOULD | Runtime supports per-project token budgets with Orchestrator-visible burn-down. | Exceeding budget pauses assignments and notifies CEO. |
| TITAN-AGT-026 | MAY | Critic may be pinned to a different provider than the artifact author (multi-model critique). | Config flag routes critic turns to a second provider in traces. |

## 9. Architecture Decision Records

**ADR-AGT-01 — Role-specialized agents instead of one generalist agent.**
*Context:* One large prompt could "do everything," and the master prompt explicitly forbids it. *Decision:* 16 role agents with narrow authority, prompts, and tool grants. *Alternatives:* single generalist (simpler, but no separation of duties, no review independence, prompt bloat); dynamic ad-hoc agent spawning (flexible, but unauditable authority). *Trade-offs:* more messages and token overhead; in exchange we get independent gates (reviewer/QA/security cannot be the author), smaller focused prompts, per-role KPIs, and mechanical enforcement of authority.

**ADR-AGT-02 — Hierarchical orchestration with cross-cutting gatekeepers, not a flat peer swarm.**
*Context:* Agents must collaborate "like a real software company." *Decision:* Tiered hierarchy (Section 3) with an Orchestrator operating the plan, plus cross-cutting gate roles. *Alternatives:* flat peer-to-peer negotiation (emergent, but deadlock-prone and hard to audit); pure pipeline (predictable, but cannot handle discussion/conflict). *Trade-offs:* the Orchestrator is a coordination bottleneck — mitigated by TITAN-AGT-009 (non-LLM watchdog restart) and by Tier-3 agents proceeding on already-assigned tasks during Orchestrator recovery.

**ADR-AGT-03 — Stateless turns with externalized memory.**
*Context:* Agents need durability, and local-first laptops need low resident cost. *Decision:* agents exist only per-turn; all state in PostgreSQL/Redis/Qdrant (TITAN-AGT-002). *Alternatives:* long-lived agent processes with in-memory context (lower per-turn hydration cost, but crash-fragile and unscalable); serialized conversation replay per turn (simple, but token cost grows unboundedly). *Trade-offs:* hydration adds latency per turn; mitigated by cached definitions, scoped retrieval instead of full replay, and Redis-cached hot context.

**ADR-AGT-04 — Strict typed message protocol over free-form agent chat.**
*Context:* "Every communication should be stored. Nothing should be lost." *Decision:* eight normative message types, strict Pydantic validation, Postgres outbox + Redis Streams. *Alternatives:* free-form natural-language chat between agents (expressive, but unparseable, unenforceable, unauditable); RPC-only calls (typed, but loses discussion/meeting semantics). *Trade-offs:* schema evolution requires versioning discipline (`schema_version` field, additive-only within a major version); expressiveness inside payloads (e.g., `position`, `summary`) remains natural language, so we keep prose where it helps and structure where it must be machine-readable.

## 10. Risks and Open Questions

- **R1 — Token-cost blowout on ceremonies and discussion threads.** Mitigation: bounded rounds (Section 5), `fast` tier for coordination roles, per-project budgets (TITAN-AGT-025). Open question: should MeetingSummary generation be batched nightly on the local profile?
- **R2 — Prompt injection through generated-project content.** A malicious dependency README could try to steer an engineer agent. Mitigation: runtime-enforced tool allow-lists (TITAN-AGT-004), provenance-tagged retrieval, Security agent scanning; full treatment in Volume 8. Open question: quarantine tier for untrusted retrieved text.
- **R3 — Reviewer/Critic sycophancy** (approving to make progress). Mitigation: anti-rubber-stamp prompt rules, rubber-stamp-rate KPI, optional multi-model critique (TITAN-AGT-026). Open question: periodic seeded-defect audits ("mutation testing" for reviewers) — proposed for Volume 10's self-evaluation loop.
- **R4 — Small local models failing structured-output schemas**, degrading the offline profile. Mitigation: self-critique retry rung, simpler per-tier output schemas. Open question: whether the `local-free` profile needs relaxed schemas with a repair pass.
- **R5 — Authority drift** — prompts and YAML `authority` lists diverging over time. Mitigation: authority section of prompts is *rendered from* the YAML (single source of truth, Section 8). Residual risk: mechanically uncheckable authority classes still rely on prompt compliance.
- **R6 — Escalation storms** — cascading blocked tasks flooding CEO/human. Mitigation: deduplication by `conflict_id`/`thread_id` in the Orchestrator watchdog; deadlock thresholds (TITAN-AGT-020). Open question: severity-based batching windows for human tripwires.

## 11. Cross-References

- **Volume 1 — Foundation:** canonical stack decisions and rationale; success metrics that agent KPIs roll up into.
- **Volume 3 — Backend Engineering:** provider-gateway implementation, Redis Streams/NATS transport details, `agent_messages` schema, WebSocket event fan-out.
- **Volume 4 — Frontend & Dashboard:** Agent Status, Live Conversations, and Kanban views rendered from this volume's messages and KPIs.
- **Volume 5 — AI Brain (Memory, Knowledge & RAG):** memory-scope semantics, retrieval pipeline, embedding and pruning mechanics behind Section 4.15.
- **Volume 6 — Engineering Workflow:** the task-state machine and end-to-end lifecycle that composes Section 5's collaboration patterns.
- **Volume 7 — Tool Ecosystem & Integrations:** the tool registry backing `tool_grants`; sandboxing of `terminal.run`, `repo.*`, and browser automation.
- **Volume 8 — Security:** threat model behind R2, secret management, audit-log requirements referenced by TITAN-AGT-024.
- **Volume 9 — Production:** observability wiring (OpenTelemetry/Prometheus/Grafana/Loki) for agent KPIs and traces.
- **Volume 10 — Autonomous Evolution:** self-evaluation, reviewer audits (R3), multi-model collaboration extending TITAN-AGT-026, and new-agent creation building on TITAN-AGT-006.

---

*End of TITAN-VOL-02 — Draft v1.0.*
