# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 10 — Future Roadmap & Autonomous Evolution

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform Specification |
| **Volume** | 10 of 10 — Future Roadmap & Autonomous Evolution |
| **Doc ID** | TITAN-VOL-10 |
| **Requirement Prefix** | TITAN-EVO-NNN |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |

**Abstract.** This volume specifies how TITAN improves itself over time without compromising safety or human control. It defines the closed-loop evolution system: per-task agent scorecards and KPI aggregation, post-task and post-project reflection loops written to organization memory, a failure taxonomy with root-cause records that feed prompt and policy updates, an A/B evaluation harness and benchmark suite for continuous optimization of prompts and model routing, multi-model debate and judge protocols, and an automated pipeline for proposing, sandbox-trialing, and promoting entirely new agents — always behind a human approval gate and full version control. It further specifies the plugin marketplace vision, enterprise capabilities (SSO, multi-tenancy, compliance, private model hosting), fine-tuning on organization memory, a phased long-term roadmap, and the known limits and open research questions of autonomous software engineering. Every mechanism in this volume is implementable on the canonical stack fixed in Volume 1 (FastAPI + PostgreSQL 16 + Redis 7 + Qdrant, provider-agnostic LLM gateway, Docker Compose locally, Kubernetes in production) and preserves TITAN's local-first, free-tier-friendly, open-source posture.

---

## Table of Contents

- [1. Introduction & Scope](#1-introduction--scope)
- [2. The Evolution Safety Model](#2-the-evolution-safety-model)
- [3. Agent Self-Evaluation: Scorecards & KPIs](#3-agent-self-evaluation-scorecards--kpis)
- [4. Reflection Loops](#4-reflection-loops)
- [5. Learning from Failures](#5-learning-from-failures)
- [6. Continuous Optimization: Prompts, Routing & Benchmarks](#6-continuous-optimization-prompts-routing--benchmarks)
- [7. AI Debates & Multi-Model Collaboration](#7-ai-debates--multi-model-collaboration)
- [8. Automated Creation of New Agents](#8-automated-creation-of-new-agents)
- [9. Plugin Marketplace Vision](#9-plugin-marketplace-vision)
- [10. Enterprise Features](#10-enterprise-features)
- [11. Model Fine-Tuning on Organization Memory](#11-model-fine-tuning-on-organization-memory)
- [12. Long-Term Roadmap](#12-long-term-roadmap)
- [13. Risks, Known Limits & Open Research Questions](#13-risks-known-limits--open-research-questions)
- [14. Cross-References](#14-cross-references)

---

## 1. Introduction & Scope

### 1.1 Purpose

Volumes 1–9 specify a platform that builds software. This volume specifies the platform that builds a *better platform*: the feedback loops, evaluation infrastructure, and governance that let TITAN's agents measurably improve week over week. The core thesis is that self-improvement is an engineering discipline, not an emergent property. Improvement only happens when (a) performance is measured per task, (b) failures are classified and traced to causes, (c) candidate changes (prompts, routing policies, new agents) are evaluated against fixed benchmarks before deployment, and (d) every change is versioned, reviewable, and reversible.

### 1.2 Design Principles

1. **Measured, not vibes.** No prompt, policy, or agent change ships without benchmark evidence (§6).
2. **Human-gated self-modification.** TITAN may *propose* changes to itself; only a human with the `evolution:approve` RBAC permission may *apply* them (§2).
3. **Everything is versioned.** Prompts, routing policies, agent specifications, and fine-tuned adapters live in the monorepo under Git; runtime state references immutable versions.
4. **Local-first.** Every mechanism in this volume — including the evaluation harness and fine-tuning — runs on a single laptop via Docker Compose using free/local tools (Ollama for local models). Cloud/scale paths are documented, never required.
5. **Fail toward the status quo.** If any evolution component is down or ambiguous, agents run the last approved configuration.

### 1.3 Terminology

| Term | Definition |
|---|---|
| **Scorecard** | Structured per-task evaluation record produced when a task closes. |
| **Reflection** | Agent-authored retrospective note persisted to memory (Volume 5). |
| **RCR** | Root-Cause Record — a structured analysis of a classified failure. |
| **Evolution Proposal (EP)** | A versioned, machine-readable proposal to change a prompt, policy, routing rule, or agent roster. |
| **TITAN-Bench** | The fixed benchmark suite used to evaluate agent/prompt/model changes. |
| **Champion / Challenger** | The currently deployed configuration vs. a candidate under A/B evaluation. |

---

## 2. The Evolution Safety Model

All capabilities in this volume are constrained by a single safety model, specified first because every later section depends on it.

### 2.1 The Evolution Control Loop

```
            +-----------------------------------------------------------+
            |                  EVOLUTION CONTROL LOOP                   |
            +-----------------------------------------------------------+

  OBSERVE            REFLECT             PROPOSE              EVALUATE
 +---------+       +-----------+       +------------+       +-------------+
 | Score-  | ----> | Reflection| ----> | Evolution  | ----> | TITAN-Bench |
 | cards,  |       | notes,    |       | Proposal   |       | offline run |
 | KPIs,   |       | RCRs      |       | (EP, Git   |       | + A/B online|
 | traces  |       | (memory)  |       | branch/PR) |       | (shadow)    |
 +---------+       +-----------+       +------------+       +------+------+
      ^                                                            |
      |                                                     pass?  v
 +----+------+       +------------+        +-----------+    +-------------+
 | MONITOR   | <---- | DEPLOY     | <----- | HUMAN     |<---| Eval report |
 | KPIs,     |       | (versioned,|        | APPROVAL  |    | attached to |
 | rollback  |       |  staged)   |        | GATE      |    | PR          |
 | triggers  |       +------------+        +-----------+    +-------------+
 +-----------+
```

Every loop iteration produces an auditable trail: telemetry → memory artifact → EP (a Git pull request in the monorepo) → benchmark report → human approval → staged deployment → post-deploy monitoring with automatic rollback triggers.

### 2.2 Requirements

**TITAN-EVO-001 (MUST) — Human approval gate for all self-modification.**
No change to a prompt template, routing policy, agent specification, guardrail, or fine-tuned model may take effect in any environment other than an isolated sandbox without explicit approval by a human holding the `evolution:approve` RBAC permission (Volume 8 RBAC model).
*Acceptance criteria:* (a) The orchestrator refuses to load any evolution artifact whose EP lacks an `approved_by` user ID and signed approval timestamp; (b) an integration test proves that an unapproved prompt version is never selected at runtime; (c) approvals are recorded in the audit log with actor, artifact hash, and diff link.

**TITAN-EVO-002 (MUST) — Version control for all evolution artifacts.**
Prompts, routing policies, agent specs, benchmark definitions, and EPs are files in the monorepo (`/evolution/**`), changed only via pull request. The PostgreSQL `evolution_artifacts` table stores only immutable references (Git commit SHA + path + content hash).
*Acceptance criteria:* (a) Runtime loads artifacts by pinned SHA; (b) `git revert` of an evolution commit followed by redeploy restores prior behavior byte-for-byte; (c) no mutable prompt text exists in the database.

**TITAN-EVO-003 (MUST) — Automatic rollback triggers.**
Every deployed evolution change carries a monitoring contract: KPI floors (e.g., task success rate, review acceptance rate) and an observation window. Breaching a floor automatically reverts to the champion configuration and opens an incident RCR.
*Acceptance criteria:* (a) Rollback executes without human action within 5 minutes of a floor breach; (b) rollback emits a Prometheus alert and audit event; (c) a chaos test demonstrates rollback on an artificially degraded challenger.

### 2.3 ADR-EVO-01 — Self-Modification via Git PRs with Human Approval

- **Context:** TITAN must improve its own prompts, policies, and agent roster, but uncontrolled self-modification risks silent capability drift, reward hacking, and unauditable behavior.
- **Decision:** All self-modification is expressed as pull requests against `/evolution/**` in the monorepo. Agents may open, benchmark, and comment on these PRs; only humans may merge. Runtime configuration pins Git SHAs.
- **Alternatives considered:** (1) *Database-stored mutable prompts with an admin UI* — faster iteration but no diff review, weak audit, easy to bypass; (2) *fully autonomous self-modification with statistical safeguards only* — maximizes learning speed but unacceptable for an open-source platform run on customer code; (3) *no self-modification, manual tuning only* — safe but abandons the volume's purpose.
- **Trade-offs:** Human gating slows the improvement loop (hours–days instead of minutes) and requires maintainer attention. We accept this: TITAN's evolution cadence is weekly, not per-request, and the Git-based flow reuses the review tooling TITAN already has (Volume 6 workflow, Volume 7 GitHub integration) at zero extra infrastructure cost.

---

## 3. Agent Self-Evaluation: Scorecards & KPIs

### 3.1 Task Scorecards

Every task that reaches a terminal state (`done`, `rejected`, `abandoned`) triggers scorecard generation by the orchestrator, combining objective telemetry with a rubric-based LLM evaluation performed by the Code Reviewer Agent (for code tasks) or the Project Manager Agent (for non-code tasks) — never by the agent that did the work.

| Dimension | Source | Scale | Example signal |
|---|---|---|---|
| Correctness | QA Agent results, CI | 0–100 | % acceptance criteria passing |
| First-pass quality | Review cycle count | 0–100 | 100 if merged with ≤1 review round |
| Rework ratio | Diff churn | 0–100 | Lines rewritten after first submission / total |
| Efficiency | OpenTelemetry spans | 0–100 | Tokens + wall-clock vs. task-class baseline |
| Cost | LLM gateway metering | USD + 0–100 | Normalized against task-class p50 cost |
| Standards compliance | Linters, Volume 8 rules | 0–100 | Violations per KLoC |
| Security | Security Agent scan | 0–100 | Findings weighted by severity |
| Collaboration | Message bus analysis | 0–100 | Blocked-time caused, handoff completeness |

**TITAN-EVO-004 (MUST) — Scorecard per terminal task.**
*Acceptance criteria:* (a) A row exists in `agent_scorecards` for 100% of terminal tasks within 60 seconds of closure; (b) each scorecard links the task ID, agent ID, prompt version SHA, model ID, and OpenTelemetry trace ID; (c) rubric-based dimensions are produced by a different agent than the task executor.

**TITAN-EVO-005 (MUST) — Scorecard schema.**

```sql
CREATE TABLE agent_scorecards (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         UUID NOT NULL REFERENCES tasks(id),
  agent_id        TEXT NOT NULL,             -- e.g. 'backend-engineer'
  agent_spec_sha  TEXT NOT NULL,             -- pinned Git SHA of agent spec
  prompt_sha      TEXT NOT NULL,
  model_id        TEXT NOT NULL,             -- e.g. 'anthropic/claude-*', 'ollama/llama3'
  task_class      TEXT NOT NULL,             -- taxonomy from Volume 6
  dimensions      JSONB NOT NULL,            -- {correctness: 92, rework: 71, ...}
  composite_score NUMERIC(5,2) NOT NULL,     -- weighted per task_class
  cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,
  tokens_in       BIGINT NOT NULL,
  tokens_out      BIGINT NOT NULL,
  wall_clock_s    INTEGER NOT NULL,
  trace_id        TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON agent_scorecards (agent_id, task_class, created_at);
```

*Acceptance criteria:* Migration exists; composite weights per task class are defined in `/evolution/scoring/weights.yaml` and versioned per TITAN-EVO-002.

### 3.2 KPI Aggregation — Agent Report Cards

Scorecards roll up into the per-agent KPIs defined in Volume 2 (each agent's specification names its KPIs; e.g., the Backend Engineer Agent's task success rate, first-pass review acceptance, and defect escape rate). A nightly job (Redis Streams consumer `evolution.kpi-rollup`) computes 7/30/90-day windows and publishes them to Grafana dashboards and to the frontend Agent Status view (Volume 4).

**TITAN-EVO-006 (MUST) — KPI rollups.**
*Acceptance criteria:* (a) Per-agent, per-task-class KPI aggregates exist for 7/30/90-day windows; (b) exposed as Prometheus metrics (`titan_agent_kpi{agent,kpi,window}`) and a FastAPI endpoint `GET /api/v1/evolution/agents/{agent_id}/kpis`; (c) values reconcile with raw scorecards within ±0.5%.

**TITAN-EVO-007 (SHOULD) — Degradation alerts.**
A sustained KPI drop (>10% vs. trailing 30-day mean for 3 consecutive days) opens an `evolution.investigation` task assigned to the Memory Agent and notifies maintainers via configured channels (Volume 7 integrations).
*Acceptance criteria:* Alert rule in Prometheus; auto-created investigation task carries the KPI series snapshot.

---

## 4. Reflection Loops

### 4.1 Post-Task Reflection

After scorecard generation, the executing agent runs a bounded reflection step (one LLM call, capped tokens) answering four questions: *What was the goal? What worked? What failed or was inefficient? What would I do differently?* The output is a structured note written to episodic memory (Qdrant collection `reflections`, metadata in PostgreSQL) per Volume 5's memory write path.

```json
{
  "kind": "post_task_reflection",
  "task_id": "…", "agent_id": "backend-engineer",
  "scorecard_id": "…",
  "went_well": ["Reused the pagination pattern from memory item mem_8121"],
  "went_poorly": ["Missed the tenant-scoping requirement; caught in review round 2"],
  "lesson": "For multi-tenant projects, verify every query against the tenancy checklist before submitting for review.",
  "proposed_change": {"type": "prompt_note", "target": "backend-engineer", "text": "…"},
  "confidence": 0.7
}
```

**TITAN-EVO-008 (MUST) — Post-task reflections persisted to memory.**
*Acceptance criteria:* (a) Reflection exists for ≥95% of terminal tasks (bounded retries; failure logged, never blocks task closure); (b) reflections are embedded and retrievable via the Volume 5 retrieval pipeline so future similar tasks surface relevant lessons; (c) reflection cost is capped at 2% of the task's LLM cost.

### 4.2 Post-Project Retrospectives

When a project reaches `released` or `cancelled`, the Project Manager Agent convenes a structured retrospective: it gathers all scorecards, reflections, incidents, and decision logs, and produces a retrospective document (stored in the project repo under `/docs/retrospectives/` and summarized into organization memory). The CEO Agent reviews and countersigns it; disagreements are recorded, not overwritten.

**TITAN-EVO-009 (MUST) — Post-project retrospective.**
*Acceptance criteria:* (a) Retrospective generated within 24h of project terminal state; (b) contains: KPI summary vs. targets, top 5 lessons, failure inventory with RCR links, reuse candidates (patterns/snippets promoted to organization memory), and ≥0 Evolution Proposals; (c) summarized embedding stored in Qdrant `org_memory` with `kind=retrospective`.

**TITAN-EVO-010 (SHOULD) — Lessons influence future planning.**
Sprint planning (Volume 6) retrieves the top-k relevant retrospective lessons for the project's domain and includes them in planner context.
*Acceptance criteria:* Planner traces show retrieved lesson IDs; an eval task in TITAN-Bench verifies a known lesson changes a plan.

---

## 5. Learning from Failures

### 5.1 Failure Taxonomy

All failures — task rejections, CI failures attributable to agents, incidents, rollbacks, guardrail trips — are classified into a fixed, versioned taxonomy (`/evolution/taxonomy/failures.yaml`):

| Code | Class | Examples |
|---|---|---|
| F-REQ | Requirement misunderstanding | Built wrong feature; missed acceptance criterion |
| F-CTX | Context/memory failure | Ignored existing pattern; lost mid-task context |
| F-GEN | Generation defect | Logic bug, hallucinated API, type error |
| F-STD | Standards violation | Broke Volume 8 conventions; skipped tests |
| F-SEC | Security defect | Injection risk, secret in code, missing authz |
| F-ORCH | Orchestration failure | Deadlocked dependency, wrong agent assigned, dropped handoff |
| F-TOOL | Tool/integration failure | Git conflict mishandled, sandbox timeout, plugin error |
| F-LLM | Model/provider failure | Refusal, truncation, rate-limit cascade |
| F-HUM | Human-input gap | Ambiguous client request that should have triggered clarification |

**TITAN-EVO-011 (MUST) — Every failure classified.**
*Acceptance criteria:* (a) Failure events carry exactly one primary code (+ optional secondaries); (b) classification is performed by the Memory Agent with a rubric prompt and is human-overridable in the dashboard; (c) unclassifiable events land in `F-UNK` and page for taxonomy review — sustained `F-UNK` >5% triggers a taxonomy-update EP.

### 5.2 Root-Cause Records (RCR)

Failures above a severity threshold (or any recurring failure signature, ≥3 occurrences in 30 days) get an RCR: a structured five-whys analysis authored by the Memory Agent, reviewed by the agent owning the failing domain.

```sql
CREATE TABLE root_cause_records (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  failure_code   TEXT NOT NULL,
  severity       TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
  task_ids       UUID[] NOT NULL,
  summary        TEXT NOT NULL,
  five_whys      JSONB NOT NULL,
  contributing   JSONB NOT NULL,      -- prompt_sha, model_id, routing rule, tool versions
  remediation    JSONB NOT NULL,      -- proposed EPs, memory writes, checklist additions
  status         TEXT NOT NULL DEFAULT 'open',  -- open|remediated|accepted_risk
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**TITAN-EVO-012 (MUST) — RCRs for high-severity and recurring failures.**
*Acceptance criteria:* (a) RCR created within 24h of qualifying failure; (b) every RCR links contributing artifact versions (prompt SHA, model, routing rule); (c) RCRs are searchable in the Memory Explorer (Volume 4).

### 5.3 From Failure to Prompt/Policy Update

Remediations that require behavior change become Evolution Proposals: a prompt patch, a policy/checklist addition, a routing change, or (rarely) an agent-spec change. Each EP references its motivating RCR(s), must pass TITAN-Bench (§6), and merges only through the human gate (§2). Remediations that are pure knowledge ("lesson learned") are written to organization memory instead — no approval needed, because memory influences but does not define behavior, and memory writes are already governed by Volume 5's provenance and pruning rules.

**TITAN-EVO-013 (MUST) — EP linkage and closure.**
*Acceptance criteria:* (a) Every high/critical RCR reaches `remediated` or `accepted_risk` within 30 days, tracked on a Grafana panel; (b) merged EPs automatically annotate their RCRs; (c) recurrence of the same failure signature after remediation reopens the RCR and flags the EP as ineffective.

**TITAN-EVO-014 (SHOULD) — Regression tests from failures.**
Each remediated F-GEN/F-STD/F-SEC failure adds a corresponding case to TITAN-Bench so the failure class cannot silently return.
*Acceptance criteria:* Bench case references the RCR ID; suite growth is reported monthly.

---

## 6. Continuous Optimization: Prompts, Routing & Benchmarks

### 6.1 Prompt Registry & Versioning

All agent prompts live in `/evolution/prompts/<agent>/<template>.md` with YAML front-matter (id, semver, variables, model constraints, eval requirements). The LLM gateway (canonical provider-agnostic layer) resolves `agent → prompt SHA` from the pinned runtime manifest `/evolution/manifest.yaml`.

**TITAN-EVO-015 (MUST) — Single source of truth for prompts.**
*Acceptance criteria:* (a) No prompt text is constructed outside the registry except for dynamic context injection through declared variables; (b) every LLM call logs its prompt SHA into the OpenTelemetry span; (c) the dashboard shows which prompt version served any given task.

### 6.2 TITAN-Bench — Benchmark Suite

TITAN-Bench is a fixed, versioned suite of agent-performance tasks executed in disposable Docker sandboxes. It runs entirely locally (small tasks against Ollama models on a laptop; full suite in CI on Kubernetes).

| Track | Contents | Graded by |
|---|---|---|
| B-CODE | 60+ scoped implementation tasks (API endpoint, schema migration, UI component) with hidden tests | Deterministic test harness |
| B-REVIEW | Seeded-defect PRs; measure defect detection precision/recall | Ground-truth defect labels |
| B-PLAN | Requirements → task graphs; measure dependency correctness, coverage | Rubric + graph checks |
| B-ARCH | Design questions with expert-written rubrics | LLM judge panel (§7) + rubric |
| B-SEC | Vulnerable code samples; detection + fix quality | Known CVE/CWE labels |
| B-REG | Regression cases generated from RCRs (TITAN-EVO-014) | Case-specific assertions |

**TITAN-EVO-016 (MUST) — Benchmark suite exists and gates evolution.**
*Acceptance criteria:* (a) `titan bench run --track all --manifest <sha>` produces a scored report (JSON + Markdown) attached to EP pull requests by CI; (b) suite content is versioned; score comparisons are only valid within a suite version; (c) a "smoke" subset (≤15 min, laptop-runnable on local models) and the full suite (CI) both exist.

**TITAN-EVO-017 (MUST) — Offline-first evaluation gate.**
No EP may enter online A/B testing until it beats or matches the champion on the relevant TITAN-Bench tracks (non-inferiority margin defined per track in `/evolution/bench/gates.yaml`).
*Acceptance criteria:* CI blocks the EP PR merge checklist until bench results are attached and pass gates.

### 6.3 A/B Evaluation Harness (Online)

After offline pass and human approval for trial, challengers run in one of two modes:

1. **Shadow mode (default):** the challenger receives copies of real task inputs, produces outputs that are scored but *never used*. Zero user-facing risk; costs are metered against the evolution budget.
2. **Canary mode:** the challenger serves a small percentage (default 10%) of eligible low-risk tasks (task classes whitelisted in the EP), with rollback triggers per TITAN-EVO-003.

```
                +------------- Task Router (Redis Streams) -------------+
                |                                                       |
        90%     v                                               10%     v
   +---------------------+                              +---------------------+
   | CHAMPION            |                              | CHALLENGER (canary) |
   | prompt vA, model M1 |                              | prompt vB, model M2 |
   +----------+----------+                              +----------+----------+
              |                                                    |
              +---------------> agent_scorecards <-----------------+
                                       |
                              sequential analysis
                              (SPRT / Bayesian) --> promote | extend | abort
```

**TITAN-EVO-018 (MUST) — A/B harness.**
*Acceptance criteria:* (a) Assignment is deterministic per task (hash-based) and recorded on the scorecard; (b) analysis uses a sequential test with pre-registered success metrics from the EP; (c) promote/abort recommendations are generated automatically but executed only via the human gate; (d) shadow mode works on Docker Compose with a single worker.

### 6.4 Routing Optimization

The LLM gateway routes each (agent, task_class) pair to a model per `/evolution/routing/policy.yaml` (e.g., architecture debates → strongest available model; lint-fix tasks → cheap local model). Scorecard data continuously estimates per-(task_class, model) quality/cost frontiers; the Memory Agent proposes routing EPs when a cheaper model is statistically non-inferior or a task class underperforms its floor.

**TITAN-EVO-019 (MUST) — Data-driven routing policy.**
*Acceptance criteria:* (a) Routing decisions are logged with policy version; (b) a monthly cost/quality frontier report is generated; (c) switching any route is a configuration-only change (canonical decision), validated by tests.

**TITAN-EVO-020 (SHOULD) — Cost budget enforcement for evolution.**
All evolution activity (reflections, bench runs, shadows, debates) is metered under a distinct budget with per-day caps configurable per deployment; local-model-only mode must be supported for zero-cost operation.
*Acceptance criteria:* Budget exhaustion degrades gracefully (skips optional reflection/shadow work, never blocks product tasks); Grafana panel tracks evolution spend.

### 6.5 ADR-EVO-02 — Offline Bench Gate Before Online A/B

- **Context:** Online experiments on real projects are slow, costly, and risk user-visible regressions; offline benchmarks are fast but can diverge from production distribution.
- **Decision:** Two-stage evaluation: TITAN-Bench (offline, mandatory, blocking) → shadow/canary A/B (online, human-approved) → promotion.
- **Alternatives considered:** (1) *Online-only* — realistic but slow and risky, and unusable for laptop-scale deployments with few tasks; (2) *offline-only* — fast but overfits the bench and misses distribution shift; (3) *interleaved evaluation on every request* — statistically efficient but doubles cost permanently.
- **Trade-offs:** Two stages add latency to the improvement loop and require bench maintenance (mitigated by TITAN-EVO-014 auto-growing the suite from real failures). Accepted: safety and reproducibility outrank iteration speed.

---

## 7. AI Debates & Multi-Model Collaboration

### 7.1 When to Convene a Debate

Debates are an *escalation mechanism*, not a default, because they multiply cost. The orchestrator convenes a debate when any trigger fires:

| Trigger | Example |
|---|---|
| High-stakes decision class | Architecture selection, data-model design, security-sensitive design (always debated) |
| Agent disagreement | Reviewer rejects the same artifact twice; Architect and Backend Engineer deadlock |
| Low confidence | Executing agent self-reports confidence < 0.5 on a critical task |
| High blast radius | Change touches ≥ N modules or a public API contract |
| Bench ambiguity | Challenger and champion within the non-inferiority margin on B-ARCH |

**TITAN-EVO-021 (MUST) — Debate triggers are policy-defined.**
*Acceptance criteria:* Triggers live in `/evolution/debate/policy.yaml`; every convened debate logs its trigger; debates never exceed the configured per-project daily cap.

### 7.2 Debate Protocol

```
 Round 0: Framing      — Orchestrator states the question, constraints, and
                          decision criteria (from Volume 1 principles).
 Round 1: Positions    — 2–3 debaters (different models via the LLM gateway,
                          e.g., Anthropic vs. OpenAI vs. local) independently
                          produce a position paper (no visibility of others).
 Round 2: Rebuttals    — Each debater receives the other positions and must
                          concede, rebut, or refine. Max 2 rebuttal rounds.
 Round 3: Judgment     — Judge protocol (§7.3) selects/synthesizes a decision.
 Output:  Decision Record (ADR format) + dissent appendix, stored in project
          decision log and organization memory.
```

**TITAN-EVO-022 (MUST) — Structured debate with persisted outcome.**
*Acceptance criteria:* (a) Debaters use distinct model IDs when ≥2 providers/models are configured (single-model deployments fall back to distinct personas with temperature variation, flagged as such); (b) output is an ADR with explicit dissent section; (c) full transcript stored and viewable in the dashboard's Live Conversations view (Volume 4).

### 7.3 Judge Protocols

Three judge protocols, selected by decision stakes:

1. **Single judge (default):** the strongest configured model, given a rubric (criteria weights from Round 0), scores each position and writes the synthesis. Cheap; used for medium-stakes debates.
2. **Panel majority:** 3 judges (distinct models where available) score independently; majority on the decision, judge disagreement recorded. Used for architecture and security decisions.
3. **Human-in-the-loop:** panel produces a recommendation; a human decides. Mandatory when the decision alters the platform itself (any EP debate) or when the client flagged the area as sensitive.

**TITAN-EVO-023 (MUST) — Judges are independent of debaters.**
*Acceptance criteria:* A judge's model+prompt configuration never equals any debater's in the same debate (verified at convening time); judges receive positions anonymized (position A/B/C) to reduce provider bias.

**TITAN-EVO-024 (SHOULD) — Debate efficacy tracking.**
Decisions produced by debates are tagged; their downstream outcomes (rework, incidents linked to the decision) feed a quarterly report on whether debates outperform single-agent decisions per decision class.
*Acceptance criteria:* Report generated; debate triggers tightened/loosened only via EP.

### 7.4 ADR-EVO-03 — Debate as Escalation, Not Default

- **Context:** Multi-model deliberation measurably improves hard decisions but costs 5–15x a single call.
- **Decision:** Convene debates only on policy triggers (§7.1); default path remains single-agent with review.
- **Alternatives considered:** (1) *Debate everything* — quality ceiling but cost-prohibitive and slow, hostile to the free-tier requirement; (2) *never debate* — cheap but repeats known single-model failure modes on high-stakes design; (3) *self-consistency sampling (same model, n samples)* — cheaper middle ground, retained as a MAY within the single-judge path, but does not diversify provider blind spots.
- **Trade-offs:** Trigger tuning becomes an ongoing responsibility (handled by TITAN-EVO-024's efficacy loop). Accepted.

---

## 8. Automated Creation of New Agents

### 8.1 Concept

TITAN's roster (Volume 2) is not final. The platform detects capability gaps — recurring failure clusters, task classes with chronically low scores, or workload patterns where one agent is a bottleneck — and proposes *new agents*. A new agent is not new code: it is a new **agent specification** (role, responsibilities, prompt set, tool grants, memory scopes, KPIs, escalation rules) executed by the existing agent runtime. This makes agent genesis a data/configuration problem with tractable safety properties.

### 8.2 Genesis Pipeline

```
 +-----------+   +-----------+   +-------------+   +-----------+   +-----------+
 | SIGNAL    |   | PROPOSAL  |   | SANDBOX     |   | HUMAN     |   | PROBATION |
 | gap       |-->| agent spec|-->| TRIAL       |-->| APPROVAL  |-->| canary in |
 | detection |   | EP (PR)   |   | bench +     |   | GATE      |   | real      |
 |           |   |           |   | synthetic   |   | (merge)   |   | projects  |
 +-----------+   +-----------+   | projects    |   +-----------+   +-----+-----+
                                 +-------------+                         |
                                                              pass 30d?  v
                                                             +----------------+
                                                             | PROMOTION to   |
                                                             | roster (or     |
                                                             | retirement)    |
                                                             +----------------+
```

**TITAN-EVO-025 (MUST) — Gap detection produces agent proposals.**
The Memory Agent runs a weekly analysis over scorecards, RCRs, and queue telemetry; when a gap matches proposal heuristics (e.g., task class with composite score < 60 for 30 days and no owning agent), it drafts an agent-spec EP including: charter, non-goals, prompt templates, minimal tool grants (least privilege per Volume 8), KPIs with targets, and estimated cost impact.
*Acceptance criteria:* Proposal EP validates against the agent-spec JSON Schema (`/evolution/agents/schema.json`); includes the motivating evidence dossier.

**TITAN-EVO-026 (MUST) — Sandbox trial before any human decision.**
The candidate agent runs in an isolated sandbox (Docker, no external network, synthetic Git remotes) against: relevant TITAN-Bench tracks, and ≥2 synthetic end-to-end projects (Volume 6 workflow executed against fixture requirements).
*Acceptance criteria:* (a) Trial report quantifies score deltas vs. the status quo roster on the target task classes; (b) sandbox provably cannot reach production systems (network policy test); (c) trial is reproducible from the EP SHA.

**TITAN-EVO-027 (MUST) — Human approval gate for roster changes.**
Adding, materially modifying, or retiring an agent requires EP merge by a human with `evolution:approve` (TITAN-EVO-001 applies).
*Acceptance criteria:* Roster loaded strictly from merged manifest; audit log records roster transitions.

**TITAN-EVO-028 (MUST) — Probation with automatic demotion.**
Newly promoted agents run 30 days in canary scope (whitelisted task classes, mandatory review of all outputs by an established agent). KPI floors breached → automatic suspension (tasks rerouted to previous owner) + RCR.
*Acceptance criteria:* Suspension path tested; probation status visible in the Agent Status dashboard.

**TITAN-EVO-029 (SHOULD) — Agent retirement.**
Agents whose task classes disappear or whose KPIs are dominated by another agent for 90 days are proposed for retirement via EP; their memory scopes are archived, not deleted.
*Acceptance criteria:* Retirement EP template exists; archived scopes remain queryable by the Memory Agent.

### 8.3 ADR-EVO-04 — Agents as Specifications, Not Arbitrary Code

- **Context:** "TITAN creates new agents" could mean generating new Python services — powerful but a severe security and maintenance hazard.
- **Decision:** New agents are declarative specifications executed by the shared, human-written agent runtime (Volume 2/3). Capability changes that require new *tools* go through the plugin system (Volume 7 / §9) with its own review path.
- **Alternatives considered:** (1) *Code-generating agent factory* — maximum flexibility, unacceptable attack surface, unbounded review burden; (2) *fixed roster forever* — safe but the platform cannot adapt to new domains; (3) *human-only agent authoring* — status quo tooling; retained as the fallback, but loses the systematic gap-detection signal.
- **Trade-offs:** Spec-driven agents are bounded by the runtime's existing capabilities; genuinely novel capabilities still need human-developed plugins/runtime features. Accepted: this is exactly the safety boundary we want.

---

## 9. Plugin Marketplace Vision

The plugin architecture (Volume 7) enables a community marketplace. This section specifies its governance; implementation lands in roadmap Phase 3 (§12).

### 9.1 Submission & Review

**TITAN-EVO-030 (MUST) — Marketplace submission pipeline.**
Plugins are submitted as Git repositories with a signed manifest (`titan-plugin.yaml`: identity, declared permission scopes, API surface, compatibility range). Automated review runs: static analysis, dependency/supply-chain scan (Volume 8), permission-scope lint (deny-by-default), sandboxed conformance tests. TITAN's own Security and Code Reviewer Agents produce an advisory review; a human marketplace maintainer approves initial listing.
*Acceptance criteria:* (a) Unsigned or scope-escalating updates are auto-rejected; (b) review verdicts and reports are public on the listing; (c) the entire pipeline is open-source so third parties can self-host a private registry.

**TITAN-EVO-031 (MUST) — Trust tiers and ratings.**

| Tier | Meaning | Requirements |
|---|---|---|
| Experimental | Listed, unvetted beyond automated scan | Automated pipeline pass |
| Community | Human-reviewed once | Maintainer approval + 30 days, ≥10 installs, no security reports |
| Verified | Publisher identity verified, re-reviewed each release | Signed releases, SLA on vulnerability response |
| Core | Maintained in the TITAN monorepo | Full Volume 8 security bar |

Ratings combine user stars with objective telemetry (opt-in): install count, crash rate, permission prompts triggered.
*Acceptance criteria:* Tier displayed at install time; installing below-Community tier requires explicit admin confirmation; telemetry is opt-in and anonymized.

**TITAN-EVO-032 (MAY) — Revenue model options.**
The marketplace remains optional and must never paywall core functionality (open-source, local-first canon). Documented options, decided by the governance body in Phase 3: (a) free-only listings (default at launch); (b) paid listings with a revenue share (e.g., 85/15 developer/platform) processed by a pluggable payment provider; (c) organization-level subscriptions bundling Verified-tier support; (d) donations/sponsorship links. Self-hosted registries never owe fees.
*Acceptance criteria:* Payment integration is a plugin itself; the platform runs fully with payments disabled.

### 9.2 ADR-EVO-05 — Open Registry Protocol over Closed Store

- **Context:** A single central store maximizes quality control but conflicts with the open-source, local-first mandate and creates a single point of governance failure.
- **Decision:** Define an open registry protocol (static-JSON index + signed artifacts, OCI-registry-compatible distribution); the official TITAN registry is one instance, and enterprises/communities can host private registries with identical tooling.
- **Alternatives considered:** (1) *Closed official store only* — better curation, unacceptable centralization; (2) *no registry, Git URLs only* — maximally open but no trust signals, poor discovery; (3) *npm/PyPI piggybacking* — reuses infrastructure but cannot carry TITAN's permission-scope and review metadata natively.
- **Trade-offs:** Federated registries fragment ratings and duplicate review effort; mitigated by making review reports portable signed artifacts. Accepted.

---

## 10. Enterprise Features

Enterprise capabilities extend — never replace — the open-source core (open-core boundaries are governance decisions recorded here; all security mechanisms build on Volume 8).

**TITAN-EVO-033 (MUST) — SSO.**
Enterprise deployments support SAML 2.0 and OIDC identity providers (Okta, Entra ID, Google Workspace, Keycloak) on top of the canonical OAuth2/OIDC + JWT auth. SCIM 2.0 provisioning maps IdP groups to TITAN RBAC roles.
*Acceptance criteria:* (a) Keycloak-based local test proves login, group→role mapping, and deprovisioning within 15 minutes of IdP removal; (b) break-glass local admin remains available and audited.

**TITAN-EVO-034 (MUST) — Multi-tenancy.**
Tenancy model: shared control plane, per-tenant isolation of data and execution. PostgreSQL row-level security keyed on `tenant_id` for all tenant tables; per-tenant Qdrant collections and Redis key prefixes; per-tenant Kubernetes namespaces (or Compose project isolation locally) for agent sandboxes; per-tenant LLM gateway credentials and budgets.
*Acceptance criteria:* (a) Automated cross-tenant access test suite passes (attempts to read another tenant's tasks/memory/artifacts all fail at the database layer, not just the API layer); (b) noisy-neighbor controls: per-tenant queue quotas on Redis Streams consumer groups; (c) tenant export/delete implements data portability and right-to-erasure.

**TITAN-EVO-035 (MUST) — Compliance foundations.**
Immutable audit log (append-only PostgreSQL table + object-storage archive) covering auth events, approvals (all §2 gates), data access, and agent actions with trace IDs; configurable data-retention policies; data-residency pinning (region-scoped storage classes); SOC 2 Type II and ISO 27001 control mappings maintained as documentation in-repo; GDPR data-processing documentation for memory contents.
*Acceptance criteria:* (a) Audit events are hash-chained and verifiable; (b) retention jobs provably purge expired data including vector embeddings; (c) a compliance-controls matrix maps each control to implementing requirements across volumes.

**TITAN-EVO-036 (MUST) — Private model hosting.**
Enterprises can route all LLM traffic to self-hosted models (Ollama or vLLM/TGI behind the same gateway interface) with a hard-egress mode: a network policy plus gateway configuration that guarantees no prompt/completion leaves the deployment boundary.
*Acceptance criteria:* (a) Hard-egress mode verified by an automated test that fails if any non-allowlisted egress occurs during a full project run; (b) switching a deployment from cloud providers to private hosting is configuration-only (canonical decision); (c) TITAN-Bench publishes reference quality/cost numbers for popular open-weight models so enterprises can set expectations.

**TITAN-EVO-037 (SHOULD) — Enterprise governance controls.**
Policy-as-code hooks: organizations can require, e.g., "human review on all F-SEC-class changes," "no canary A/B on production-tagged projects," "debates mandatory for data-model changes." Policies are versioned files evaluated by the orchestrator.
*Acceptance criteria:* Policy violations block the action and are audited; policy files validate against a published schema.

---

## 11. Model Fine-Tuning on Organization Memory

### 11.1 Purpose and Boundaries

After months of operation, an organization's memory (Volume 5) contains thousands of high-quality (context → action → outcome) triples. Fine-tuning small local models on this corpus can raise quality on organization-specific conventions and cut cost/latency for routine task classes. Fine-tuning is strictly optional, off by default, and never a substitute for the memory/RAG pipeline — it complements it for style, conventions, and routing-cheap task classes.

### 11.2 Data Pipeline

```
 org memory + scorecards
        |
        v
 [1] SELECT   — only samples from tasks with composite_score >= 85,
                no open RCRs, review-approved artifacts
        |
 [2] SCRUB    — secret scanning, PII removal, license filter,
                tenant-consent check (per-project data-use flags)
        |
 [3] FORMAT   — instruction-tuning pairs / preference pairs
                (chosen = merged artifact, rejected = pre-review draft)
        |
 [4] DEDUPE   — near-duplicate removal, per-task-class balancing
        |
 [5] FREEZE   — dataset manifest (content hashes) committed via EP
        |
        v
 LoRA fine-tune (local GPU or rented) --> adapter artifact (versioned)
        |
        v
 TITAN-Bench full run + safety eval  --> human approval --> gateway route
```

**TITAN-EVO-038 (MUST) — Consent-scoped, scrubbed training data.**
*Acceptance criteria:* (a) Samples originate only from projects whose data-use policy allows training (per-tenant, per-project flag, default OFF); (b) secret/PII scrubbing runs with a published detector list and a quarantine queue for uncertain samples; (c) the frozen dataset manifest makes any training run reproducible and auditable; (d) client code marked proprietary is excluded regardless of score.

**TITAN-EVO-039 (MUST) — Evaluation and safety gates for tuned models.**
A tuned adapter ships only if it: matches or beats its base model on the full TITAN-Bench; shows no regression on the safety eval set (guardrail-adherence probes, injection-resistance probes from Volume 8); and passes a memorization audit (canary-string extraction and near-verbatim training-sample regurgitation checks).
*Acceptance criteria:* All three gates automated in the tuning pipeline; results attached to the model's EP; deployment via gateway routing EP (human gate applies).

**TITAN-EVO-040 (MUST) — Tuned models are routed, versioned artifacts.**
Adapters are content-addressed artifacts referenced by the routing policy; rollback is a routing change (TITAN-EVO-003 applies).
*Acceptance criteria:* `ollama`-servable adapter format supported for local use; per-adapter lineage (base model, dataset manifest, training config) queryable via API.

**TITAN-EVO-041 (MAY) — Provider-hosted fine-tuning.**
Where a cloud provider offers fine-tuning APIs, the same pipeline MAY target them, subject to the tenant's data-residency and egress policies (TITAN-EVO-036 hard-egress mode forbids it).
*Acceptance criteria:* Provider tuning is a gateway-plugin capability; disabled by default.

### 11.3 ADR-EVO-06 — LoRA on Local Open-Weight Models as the Default Tuning Path

- **Context:** Fine-tuning must be affordable, private, and reproducible for a local-first open-source project; frontier-model tuning APIs are provider-locked and move data off-premises.
- **Decision:** Default path is parameter-efficient LoRA/QLoRA tuning of open-weight models (7–14B class), producing adapters servable via Ollama through the existing gateway. Provider-hosted tuning is optional (TITAN-EVO-041).
- **Alternatives considered:** (1) *Full fine-tunes* — better ceiling, but cost/VRAM-prohibitive for most users and slower to iterate; (2) *provider tuning APIs as default* — simplest ops but breaks local-first and hard-egress guarantees; (3) *no tuning, RAG-only* — simplest and remains the default *runtime* behavior, but leaves measurable cost/latency wins for routine tasks unrealized.
- **Trade-offs:** Small tuned models will not out-reason frontier models; they are targeted at conventions and routine task classes selected by the routing frontier data (§6.4). Accepted.

---

## 12. Long-Term Roadmap

Phases assume Volumes 1–9 deliver the MVP platform ("Phase 0"). Dates are relative to MVP GA; each phase's exit criteria are the listed requirement IDs reaching "implemented + verified."

| Phase | Horizon | Theme | Milestones (exit criteria) |
|---|---|---|---|
| **1 — Instrumented** | GA + 3 months | Measure everything | Scorecards & KPIs live (EVO-004..007); reflections persisted (EVO-008..010); failure taxonomy + RCRs (EVO-011..013); prompt registry pinned (EVO-015); TITAN-Bench smoke track in CI (EVO-016 partial) |
| **2 — Optimizing** | GA + 6 months | Close the loop | Full TITAN-Bench + gates (EVO-016, 017); A/B harness shadow+canary (EVO-018); routing frontier reports (EVO-019, 020); debate protocol for architecture/security decisions (EVO-021..024); regression-from-failure pipeline (EVO-014) |
| **3 — Extensible** | GA + 12 months | Ecosystem | Agent genesis pipeline end-to-end with first community-proposed agent promoted (EVO-025..029); marketplace registry protocol + Experimental/Community tiers (EVO-030, 031); revenue-model decision by governance (EVO-032) |
| **4 — Enterprise** | GA + 18 months | Trust at scale | SSO/SCIM (EVO-033); hardened multi-tenancy with cross-tenant test suite (EVO-034); audit/compliance matrix, SOC 2 readiness (EVO-035); private model hosting hard-egress mode (EVO-036); policy-as-code (EVO-037) |
| **5 — Learning** | GA + 24 months | Organization-tuned intelligence | Fine-tuning pipeline with all safety gates (EVO-038..041); tuned local models serving ≥30% of routine task volume in reference deployments; documented cost reduction ≥40% on those classes at non-inferior quality |
| **6 — Frontier** | GA + 24 months+ | Research program | Items from §13 pursued as public RFCs: cross-organization federated pattern learning, long-horizon multi-project planning, formal verification of agent guardrails |

**TITAN-EVO-042 (SHOULD) — Public roadmap tracking.**
The roadmap is maintained as a living document in-repo with per-milestone status, linked issues, and quarterly public review notes.
*Acceptance criteria:* Each phase milestone maps to tracked issues; status auto-generated from requirement verification records.

---

## 13. Risks, Known Limits & Open Research Questions

### 13.1 Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Metric gaming / reward hacking** — agents optimize scorecard dimensions (e.g., minimizing review rounds by under-reporting uncertainty) rather than true quality | Medium | High | Cross-agent scoring (never self-scored), hidden bench tests, periodic human audit of scorecard–outcome correlation, KPI definitions changeable only via EP |
| R2 | **Benchmark overfitting** — prompts evolve to ace TITAN-Bench while degrading on real work | Medium | High | Online A/B stage is mandatory (ADR-EVO-02); bench refresh from real RCRs (EVO-014); held-out private test split rotated quarterly |
| R3 | **Evolution loop instability** — oscillating prompt/routing changes | Low | Medium | Sequential-test promotion thresholds, cool-down windows between EPs touching the same artifact, rollback floors (EVO-003) |
| R4 | **Approval fatigue** — humans rubber-stamp EPs, hollowing out the safety gate | Medium | High | EPs carry auto-generated risk summaries and diffs; batch low-risk changes; governance requires two approvers for roster/model changes |
| R5 | **Training-data leakage** — secrets or proprietary code in tuned models | Low | Critical | Default-OFF consent flags, scrubbing + quarantine, memorization audit gate (EVO-039), hard-egress mode for enterprises |
| R6 | **Marketplace supply-chain attack** — malicious plugin exfiltrates code | Medium | Critical | Signed manifests, deny-by-default scopes, sandboxed execution (Volume 7), trust tiers with human review (EVO-030, 031) |
| R7 | **Cost blow-up from evolution overhead** — reflections, debates, shadows consuming budget | Medium | Medium | Dedicated evolution budget with graceful degradation (EVO-020); local-model-only mode |
| R8 | **Debate theater** — multi-model debates converge on confident consensus that is still wrong | Medium | Medium | Dissent is mandatory output; efficacy tracking (EVO-024) can demote debate from decision classes where it adds no measured value |

### 13.2 Known Limits

1. **TITAN cannot verify its own alignment.** Guardrail adherence is tested empirically (probes, audits), not proven. Formal verification of agent policies is a research goal, not a shipped capability.
2. **Small-sample statistics.** Laptop-scale deployments generate too few tasks for online A/B significance; they rely on offline bench results plus community-shared (opt-in, anonymized) evaluation data.
3. **Taxonomy lag.** Genuinely novel failure modes will first appear as `F-UNK`; the taxonomy update loop takes days, during which classification quality drops.
4. **Spec-bounded agent genesis.** New agents cannot exceed the runtime's tool surface (ADR-EVO-04); capability breakthroughs still require human engineering.
5. **Tuned-model scope.** Fine-tuned local models improve conventions and routine work; they do not close the reasoning gap with frontier models on hard design tasks.

### 13.3 Open Research Questions

1. **Credit assignment across agents:** when a project fails, how should blame/credit propagate through a multi-agent task graph? Current per-task scorecards approximate this poorly for emergent, cross-task failures.
2. **Federated organizational learning:** can organizations share learned patterns (not data) — e.g., differentially private prompt deltas or adapter merging — without leaking proprietary information?
3. **Long-horizon planning evaluation:** TITAN-Bench measures task- and project-scale quality; no accepted benchmark exists for multi-month, multi-project portfolio behavior.
4. **Judge reliability:** LLM judges exhibit position, verbosity, and self-preference biases; anonymization (EVO-023) mitigates but does not eliminate them. What audit rate of human re-judging is sufficient?
5. **Safe autonomy dial:** under what measured conditions (if any) could specific low-risk EP classes graduate to automated approval — and how would that decision itself be validated and reversed? (Position of this spec: not before Phase 6, and only via public RFC.)

---

## 14. Cross-References

| Volume | Relationship to this volume |
|---|---|
| **Volume 1 — Foundation** | Owns the canonical stack and design principles referenced throughout; success metrics that evolution KPIs roll up into |
| **Volume 2 — Multi-Agent Intelligence** | Defines each agent's KPIs consumed by §3; agent specification format extended by the genesis pipeline (§8); Memory Agent responsibilities |
| **Volume 3 — Backend Engineering** | FastAPI services, Redis Streams consumers, and PostgreSQL schemas that host scorecards, RCRs, and the A/B router |
| **Volume 4 — Frontend & Dashboard** | Agent Status, Memory Explorer, and Live Conversations views that surface scorecards, RCRs, debates, and EP status |
| **Volume 5 — Memory, Knowledge & RAG** | Write/retrieval paths for reflections and retrospectives (§4); provenance and pruning rules governing lesson memory; source corpus for fine-tuning (§11) |
| **Volume 6 — Engineering Workflow** | Task lifecycle emitting scorecard triggers; planning stage consuming retrospective lessons; synthetic-project fixtures used in sandbox trials |
| **Volume 7 — Tool Ecosystem & Integrations** | Plugin architecture and sandboxing that the marketplace (§9) governs; GitHub integration carrying EP pull requests |
| **Volume 8 — Security** | RBAC permissions (`evolution:approve`), audit logging, supply-chain scanning, secret/PII detection used by §2, §9, §10, §11 |
| **Volume 9 — Deployment & Production** | Prometheus/Grafana/OpenTelemetry infrastructure behind KPI rollups and rollback triggers; canary deployment machinery reused by the A/B harness |

---

*End of Volume 10 — Future Roadmap & Autonomous Evolution (TITAN-VOL-10, Draft v1.0, 2026-07-04).*
