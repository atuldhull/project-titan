# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 1 — Foundation & Product Requirements

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform (10 volumes) |
| **Volume** | 1 — Foundation & Product Requirements |
| **Doc ID** | TITAN-VOL-01 |
| **Requirement Prefix** | TITAN-FND-NNN |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |
| **Audience** | All TITAN contributors (human and AI agents); readers of Volumes 2–10 |

**Abstract.** This volume is the root document of the PROJECT TITAN specification series. It defines what TITAN is (a production-grade, open-source, local-first multi-agent platform that autonomously designs, builds, tests, reviews, and deploys software from a natural-language request), why it exists, who it serves, and what it must do. It contains the numbered functional and non-functional requirements all other volumes trace to, the user stories and success metrics that define "done," the roadmap from MVP to v2, the high-level architecture, and — uniquely in this series — the authoritative justification of the canonical technology stack via trade-off analysis and ADR-style decision records. Volumes 2–10 reference these decisions; they do not re-litigate them.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Vision, Mission, and Problem Statement](#2-vision-mission-and-problem-statement)
3. [User Personas](#3-user-personas)
4. [Functional Requirements](#4-functional-requirements)
5. [Non-Functional Requirements](#5-non-functional-requirements)
6. [User Stories and Acceptance Criteria](#6-user-stories-and-acceptance-criteria)
7. [Success Metrics and KPIs](#7-success-metrics-and-kpis)
8. [Constraints](#8-constraints)
9. [Product Roadmap](#9-product-roadmap)
10. [High-Level System Architecture](#10-high-level-system-architecture)
11. [Technology Stack Selection and Justification](#11-technology-stack-selection-and-justification)
12. [Development Philosophy and Engineering Principles](#12-development-philosophy-and-engineering-principles)
13. [Risks and Open Questions](#13-risks-and-open-questions)
14. [Cross-References](#14-cross-references)
15. [Glossary of TITAN Terms](#15-glossary-of-titan-terms)

---

## 1. Introduction

### 1.1 Purpose

This document is the product foundation for PROJECT TITAN. Every agent design, service, screen, workflow, and security control in Volumes 2–10 must trace to a requirement here. If a later volume needs new scope, it proposes an amendment to this volume rather than inventing scope silently.

### 1.2 Scope of This Volume

In scope: vision and mission; problem definition; personas; functional and non-functional requirements; user stories; KPIs; constraints; roadmap; high-level architecture; canonical stack justification; engineering philosophy; series-wide glossary. Out of scope (owned elsewhere): per-agent specs (Vol. 2), backend service design (Vol. 3), UI screen specs (Vol. 4), memory/RAG internals (Vol. 5), workflow state machines (Vol. 6), integrations (Vol. 7), security controls (Vol. 8), production operations (Vol. 9), evolution features (Vol. 10).

### 1.3 Conventions

- Requirement IDs use the form `TITAN-FND-NNN`; IDs are never reused or renumbered.
- Priorities follow RFC 2119 semantics: **MUST** (release-blocking), **SHOULD** (expected unless an ADR justifies otherwise), **MAY** (optional).
- Every requirement carries acceptance criteria (AC) testable by a human or an AI agent without further interpretation.

---

## 2. Vision, Mission, and Problem Statement

### 2.1 Vision

A user types "Build me a Netflix clone," "Build a Banking System," "Build a Hospital Management System," or "Build an AI SaaS" — and receives a production-ready application built by a coordinated organization of specialized AI agents. The human acts only as the client: answering essential clarification questions and approving releases. Everything else — requirements analysis, architecture, task breakdown, implementation, review, testing, security scanning, documentation, deployment — happens autonomously.

### 2.2 Mission

Build not one giant AI, but **an organization**: 15–20 specialized agents (CEO, Product Manager, Project Manager, System Architect, Backend Engineer, Frontend Engineer, Database Engineer, DevOps, QA, Security, Code Reviewer, Documentation, Research, Memory, and peers) — each with a role, responsibilities, memory, tools, communication abilities, decision authority, and a review process — collaborating like a real software company. TITAN is open source, runs fully on one laptop with free/local tools, and scales to enterprise Kubernetes without code changes.

### 2.3 The Problem TITAN Solves

| # | Problem | Consequence |
|---|---|---|
| P1 | Single-shot LLM codegen produces demos, not products | No tests, no review, no architecture; code decays immediately |
| P2 | Coding assistants are single-threaded pair programmers | One context window cannot hold requirements, architecture, QA, and ops at once |
| P3 | AI output is not reviewed the way human code is | No critique loop or rejection path; quality equals the first sample |
| P4 | AI tools forget everything between sessions | No project memory, no reusable patterns, no lessons learned |
| P5 | Vendor lock-in to one LLM provider | Cost, availability, and privacy constraints differ per user |
| P6 | Cloud-only tooling excludes students and privacy-sensitive teams | Zero-budget and air-gapped users are shut out |

TITAN answers P1–P4 with the **Self-Improvement Loop** (Implementation → Review → Critique → Refactor → Testing → Security Scan → Performance Analysis → Approval → Merge), executed by distinct agents that never accept the first draft; P5 with the LLM Gateway (ADR-007); P6 with the local-first hard constraint (TITAN-FND-060, C-1).

---

## 3. User Personas

### 3.1 Persona Summary

| Persona | Canonical name | Environment | Primary Goal | Key Constraint |
|---|---|---|---|---|
| Solo developer / freelancer | **Dana** | Laptop, Docker Compose, one cloud LLM key + Ollama | Ship client projects 5–10× faster | Predictable cost per project |
| Startup team (2–10 eng.) | **Arjun's team** | Shared server / small K8s, cloud LLMs | Multiply output; keep quality bar | RBAC, shared memory, GitHub flow |
| Enterprise platform org | **Meridian Corp** | On-prem/VPC Kubernetes, approved model list | Autonomous tooling under compliance | SSO/OIDC, audit logs, data residency |
| Student / hobbyist | **Priya** | Laptop, 16 GB RAM, Ollama only, zero budget | Learn by watching an AI company work | Must run 100% free and offline |

### 3.2 Persona Narratives

**Dana** submits a client brief, answers a handful of clarification questions, and reviews a working staging deployment hours later. She reads the Code Reviewer Agent's comments to keep final accountability, and she watches the cost meter: projected and actual LLM spend per project, before and during a run (TITAN-FND-021/024).

**Arjun's team** uses TITAN as a force multiplier: agents draft services, tests, and infra; humans review PRs in their real GitHub org. They need multi-user auth, per-project RBAC, shared organizational memory, and Slack notifications — and they are the reason the NATS JetStream scale-out path exists (ADR-005).

**Meridian Corp** runs TITAN inside its VPC. Only approved providers (a private endpoint plus local models via Ollama) may be called — enforced by gateway configuration, not code. Every agent action lands in an immutable audit log (Vol. 8); deployment is Kubernetes with the full observability stack (Vol. 9).

**Priya** has no credit card. She runs `docker compose up`, points the LLM Gateway at Ollama, and watches the agent organization plan and build a to-do app — reading every inter-agent message in Live Conversations to learn how engineering teams work. If any TITAN feature requires a paid service, TITAN has failed this persona (C-1).

---

## 4. Functional Requirements

Grouped by capability. Priorities: MUST / SHOULD / MAY. Targets map to Section 9.

### 4.1 Project Intake and Planning

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-001 | Accept a free-text project brief via UI and API, creating a Project entity. | MUST | MVP | POST `/api/v1/projects` returns 201 with ID; project visible on dashboard ≤ 2 s. |
| TITAN-FND-002 | CEO Agent asks only essential clarification questions (≤ 7 at MVP); skipping them still yields a plan using documented defaults. | MUST | MVP | Standard brief → ≤ 7 questions; skip-all path produces a valid plan. |
| TITAN-FND-003 | Decompose the brief into milestones, epics, and tasks with dependencies (a task DAG). | MUST | MVP | Plan passes cycle check; every task has owner role, estimate, ≥ 1 AC. |
| TITAN-FND-004 | Auto-generate per project: roadmap, Kanban, task graph, dependency graph, sprint plan, release plan, architecture diagram, risk analysis, tech-debt report. | SHOULD | v1 (Kanban/task graph/roadmap: MVP) | Each artifact retrievable via API, rendered in UI, refreshed on plan change. |

### 4.2 Agent Organization and Communication

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-005 | Run a roster of specialized agents (MVP: CEO, PM, Architect, Backend, Frontend, QA, Code Reviewer; v1: full 15–20 roster per Vol. 2). | MUST | MVP | Registry lists agents with role/status/current task; each MVP role completes ≥ 1 task in the E2E demo. |
| TITAN-FND-006 | Agents communicate only via typed structured messages: Task Assignment, Progress Report, Review Request, Architecture Discussion, Bug Report, Decision Log, Meeting Summary, Conflict Resolution. | MUST | MVP | Every message validates against a published Pydantic schema; malformed messages rejected and logged. |
| TITAN-FND-007 | Every communication is persisted; nothing is lost. | MUST | MVP | Kill orchestrator mid-project; on restart, message history is intact in PostgreSQL and the project resumes. |
| TITAN-FND-008 | Agents have defined decision authority and escalation paths (Architect binds Engineers; ties escalate to CEO Agent). | MUST | v1 | Staged disagreement yields a Conflict Resolution message and a logged binding decision within one cycle. |

### 4.3 Task Orchestration

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-009 | Tasks auto-create, assign, prioritize, estimate, review, complete, and reopen when review fails. | MUST | MVP | A task rejected by the Code Reviewer transitions to `reopened` with critique attached and re-executes without human action. |
| TITAN-FND-010 | Independent tasks execute in parallel under a configurable concurrency limit. | MUST | MVP | Concurrency 3 with 5 independent tasks → exactly 3 concurrent; wall time < sequential baseline. |
| TITAN-FND-011 | Task queue on Redis Streams with consumer groups; at-least-once delivery with idempotent handlers. | MUST | MVP | Killing a worker mid-task redelivers to another worker; no task lost or duplicated in effect. |

### 4.4 Code Generation and the Self-Improvement Loop

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-012 | Generate frontend, backend, database, infrastructure, tests, docs, deployment, scripts, and config into a versioned per-project git workspace. | MUST | MVP | Demo project yields a repo that builds and runs via its own `docker compose up`; all code agent-authored. |
| TITAN-FND-013 | Every component passes the Self-Improvement Loop; the first draft is never merged directly. | MUST | MVP (Security Scan + Perf Analysis stages: v1) | Any merged change shows ≥ 1 review cycle with critique and a diff from draft 1. |
| TITAN-FND-014 | Generated projects include unit, integration, and E2E tests; QA Agent gates merges on passing tests. | MUST | MVP (E2E: v1) | Demo project ≥ 60% line coverage; failing tests block `approved`. |
| TITAN-FND-015 | Security Agent scans code and dependencies (SAST + dependency audit) and can block a release. | MUST | v1 | Known-vulnerable dependency in a fixture produces a blocking finding. |

### 4.5 Memory and Knowledge

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-016 | Short-term (context), project, and organizational memory; all persistent memory survives restart. | MUST | MVP (org learning loop: v1) | Post-restart, an agent answers "what architecture did we choose and why" from memory, not chat history. |
| TITAN-FND-017 | Semantic retrieval over memories, code, and docs via Qdrant with a swappable, local-by-default embedding model. | MUST | MVP | "How did we implement auth last time" returns the prior decision record in top-3 on the fixture corpus. |
| TITAN-FND-018 | Maintain a knowledge base: coding standards, architecture patterns, best practices, API references, decision history, reusable templates. | SHOULD | v1 | Agents cite entries by ID in Decision Logs; entries are versioned and editable in UI. |

### 4.6 Multi-LLM Support

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-019 | All model calls pass through a provider-agnostic LLM Gateway supporting Anthropic, OpenAI, Google, and local models via Ollama. | MUST | MVP | Same agent task completes on Anthropic and on Ollama with only a config change. |
| TITAN-FND-020 | Provider/model selection is configuration-only and per-agent-role overridable. | MUST | MVP | Changing `agents.architect.model` reroutes that agent's calls (verified in gateway logs). |
| TITAN-FND-021 | Gateway records tokens, latency, and cost per call, aggregated per agent, task, and project. | MUST | MVP | Dashboard spend within 5% of provider-reported usage in a controlled test. |

### 4.7 User Interface

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-022 | Professional, responsive, dark-mode-first dashboard: Organization View, Projects, Tasks/Kanban, Agent Status, Live Conversations, Logs, Architecture Viewer, Deployment Dashboard, Monitoring, Memory Explorer, Plugin Manager, Settings. | MUST (MVP subset: Projects, Kanban, Agent Status, Live Conversations, Logs, Settings) | MVP/v1 | Screens exist per Vol. 4; Lighthouse a11y ≥ 90; usable at 375 px width. |
| TITAN-FND-023 | Agent activity streams to the UI in real time over WebSockets. | MUST | MVP | Agent message appears in Live Conversations ≤ 1 s (p95, local). |
| TITAN-FND-024 | Show projected and running LLM cost per project, with a budget cap that pauses the project when exceeded. | MUST | v1 (display: MVP) | $5 cap halts new LLM calls within one cycle of being reached; project enters `paused_budget`. |

### 4.8 Plugins, Integrations, and Deployment of Generated Apps

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-025 | Plugin architecture for integrations (GitHub, GitLab, Docker, Slack, Discord, Jira, Linear, Figma, VS Code, browser automation, MCP servers) with no core changes. | MUST (architecture); SHOULD (specific plugins, Vol. 7) | v1 | A sample plugin installs, registers tools, and is invoked without modifying core packages. |
| TITAN-FND-026 | GitHub integration: push generated repos, open PRs, mirror internal review as PR comments. | SHOULD | v1 | Demo project appears as a repo with ≥ 1 merged agent-authored PR carrying review comments. |
| TITAN-FND-027 | Deploy generated apps at minimum to local Docker Compose; cloud targets via plugins. | MUST (local); MAY (cloud, v2) | MVP | "Deploy" yields a reachable local URL; deployment logs stream to UI. |
| TITAN-FND-028 | Configurable human approval gates per project (release, budget, destructive actions). | MUST | MVP | With `release_approval: human`, release blocks until Approve; approval recorded with identity and timestamp. |

### 4.9 Platform Administration

| ID | Requirement | Priority | Target | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-029 | Multi-user auth via OAuth2/OIDC with JWT sessions; single-user local mode needs no external IdP. | MUST | MVP (local mode); v1 (OIDC) | Local mode works offline with a bootstrap admin; v1 logs in via Keycloak (reference OIDC IdP). |
| TITAN-FND-030 | RBAC with at least Owner, Maintainer, Viewer per organization and project. | MUST | v1 | Viewer mutations return 403, enforced server-side. |

---

## 5. Non-Functional Requirements

| ID | Category | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|---|
| TITAN-FND-050 | Performance | API reads p95 < 200 ms; WS delivery p95 < 1 s; scheduling overhead < 250 ms per task transition (excluding LLM latency). | MUST | Load test, 50 concurrent dashboard users on reference hardware, meets targets. |
| TITAN-FND-051 | Scalability | Scale from 1 laptop to K8s running ≥ 100 concurrent agent tasks across ≥ 20 projects by adding workers — config only (queue swap per ADR-005 allowed). | MUST | 3-node cluster sustains 100 concurrent tasks, < 5% failure rate. |
| TITAN-FND-052 | Reliability | No committed data loss on crash: PostgreSQL is the system of record; queue redelivery covers in-flight work; projects always resume after full restart. | MUST | Chaos test (kill any one service) loses no acknowledged data; local recovery ≤ 60 s. |
| TITAN-FND-053 | Reliability | Graceful LLM-provider degradation: retry with backoff, fail over to configured fallback, pause (not corrupt) projects if all providers are down. | MUST | Simulated outage fails over within 3 retries; no fallback → `paused_provider`, clean resume. |
| TITAN-FND-054 | Cost | A CRUD web-app project completes on the local-only profile at $0 LLM spend; cloud-profile cost is visible and capped. | MUST | Fixture completes end-to-end on Ollama with zero outbound paid calls (network capture). |
| TITAN-FND-060 | Local-first | Full platform (UI, orchestrator, datastores, local LLM path) runs on one laptop via `docker compose up`, ≤ 16 GB RAM excluding model, fully offline after pulls. | MUST | Fresh clone → running platform ≤ 15 min on reference hardware, network then disabled. |
| TITAN-FND-061 | Observability | All services emit OTel traces, Prometheus metrics, structured logs to Loki; Grafana health dashboard ships preconfigured. | MUST | One agent task is followable as a single distributed trace: API → queue → agent → gateway → DB. |
| TITAN-FND-062 | Security | Secure defaults: secrets via env/secret store only; TLS in production; audit log of privileged actions; OWASP ASVS L1 at v1, L2 at v2 (Vol. 8). | MUST | Secret-scanning CI gate passes; Vol. 8 checklist signed off before v1. |
| TITAN-FND-063 | Portability | No hard dependency on any cloud vendor or paid service in the core platform. | MUST | Build + E2E pass with no cloud credentials configured. |
| TITAN-FND-064 | Maintainability | Monorepo, strict typing (`tsc --strict`, `mypy --strict`), enforced lint/format, ≥ 80% coverage on core orchestration code. | SHOULD | CI blocks merges violating typing, lint, or coverage gates. |

---

## 6. User Stories and Acceptance Criteria

**US-1 (Priya — local-first run).** *As a student with no budget, I want to run all of TITAN offline with a local model, so I can learn how an AI engineering organization works for free.*
- **Given** Docker and a pulled Ollama model, **when** I run `docker compose up` and open the dashboard, **then** I can create a project and watch agents build it with zero paid API calls, every inter-agent message visible live. (TITAN-FND-019, -023, -054, -060)

**US-2 (Dana — brief to running app).** *As a freelancer, I want to submit a brief and receive a reviewed, tested, locally deployed application.*
- **Given** a CRUD-app brief, **when** I answer ≤ 7 questions and approve the plan, **then** TITAN produces a git repo with tests and a running local URL, and every merged change shows ≥ 1 review cycle. (TITAN-FND-001, -002, -012, -013, -027)

**US-3 (Dana — cost control).** *As a solo dev paying per token, I want a per-project budget cap so a runaway loop cannot surprise my bill.*
- **Given** a $5 cap, **when** tracked cost reaches $5, **then** the project pauses within one cycle and notifies me; raising the cap resumes it. (TITAN-FND-021, -024)

**US-4 (Arjun's team — GitHub-native).** *As a startup engineer, I want TITAN's work delivered as GitHub PRs with the agents' review discussion attached.*
- **Given** the GitHub plugin, **when** an agent completes a task, **then** a PR opens with the change, the critique thread, and passing CI. (TITAN-FND-025, -026)

**US-5 (Arjun's team — access control).** *As a team lead, I want RBAC so a contractor can watch progress but not approve releases.*
- **Given** a Viewer-role user, **when** they attempt any mutation, **then** the API returns 403 and the attempt is audit-logged. (TITAN-FND-029, -030, -062)

**US-6 (Meridian — provider governance).** *As an enterprise platform owner, I want to restrict TITAN to approved providers via configuration so no data leaves our boundary.*
- **Given** a gateway config allowing only `ollama` and one private endpoint, **when** any agent requests a model, **then** only allowed providers are called (egress audit), and gateway config edits require Owner role. (TITAN-FND-019, -020, -030)

**US-7 (All — resumability).** *As any user, I want projects to survive a crash or reboot so hours of agent work are never lost.*
- **Given** a project mid-build, **when** the host restarts, **then** the project resumes from its last consistent state with full message history. (TITAN-FND-007, -052)

**US-8 (Priya — learning by inspection).** *As a learner, I want to open the Memory Explorer and Decision Log to understand why the architecture was chosen.*
- **Given** a completed project, **when** I open its Decision Log, **then** each major choice shows context, alternatives, and rationale in ADR form. (TITAN-FND-016, -018)

---

## 7. Success Metrics and KPIs

| KPI | Definition | MVP | v1 | v2 |
|---|---|---|---|---|
| Autonomous completion rate | % of the fixed 10-brief benchmark suite producing a running, test-passing app with no human edits | ≥ 50% | ≥ 75% | ≥ 90% |
| First-draft rejection rate | % of implementation tasks reopened ≥ once by review (proves the loop bites) | ≥ 30% | 30–60% | 30–60% |
| Time-to-first-deploy | Median wall time, brief → local deployment, CRUD benchmark, cloud profile | ≤ 4 h | ≤ 1 h | ≤ 30 min |
| Local-only viability | Benchmark briefs completing on the Ollama-only profile | ≥ 3/10 | ≥ 6/10 | ≥ 8/10 |
| Cost per benchmark project | Median gateway-tracked spend, cloud profile | ≤ $15 | ≤ $8 | ≤ $5 |
| Generated test coverage | Median line coverage of generated projects | ≥ 60% | ≥ 75% | ≥ 80% |
| Crash recovery | Orchestrator recovery success in the chaos suite | 100% | 100% | 100% |
| Community adoption | GitHub stars / external contributors | n/a | 2,000 / 10 | 10,000 / 50 |
| Setup friction | Fresh clone → running platform, reference laptop | ≤ 15 min | ≤ 10 min | ≤ 10 min |

Anti-goal: **human intervention count** (clarifications + approvals) is displayed but not driven to zero — approval gates (TITAN-FND-028) are a feature, not friction.

---

## 8. Constraints

| # | Constraint | Type | Consequence |
|---|---|---|---|
| C-1 | **Local-first and free-tier-friendly is a hard requirement**: the core platform runs fully on one laptop with free/local tools; no feature may mandate a paid service. | Product (user-imposed, non-negotiable) | Every design in Vols. 2–10 includes a local profile; paid/cloud paths are optional. |
| C-2 | Open-source project (licence selection tracked as Q1). | Legal | No closed-source dependencies in core. |
| C-3 | Reference hardware: 4-core/16 GB laptop; reference cluster: 3 × 4-vCPU/16 GB nodes. | Technical | All NFR targets measured against these. |
| C-4 | LLM non-determinism and context limits are environmental facts. | Technical | Review loop tolerates variable quality; context bounded per call (Vol. 5). |
| C-5 | The canonical stack (Section 11) is fixed series-wide; changes require an ADR amendment here. | Process | Prevents drift among 10 parallel volume authors. |
| C-6 | Cloud deployment targets for generated apps are plugin territory (Vol. 7), not core. | Scope | Keeps core cloud-neutral (TITAN-FND-063). |
| C-7 | OSS contributors have intermittent availability. | Process | Volumes must be implementable without oral tradition. |

---

## 9. Product Roadmap

### 9.1 MVP — "The Loop Works" (M1)

Prove the thesis end-to-end on one laptop: intake, clarification, and planning (TITAN-FND-001–003); 7-agent roster (TITAN-FND-005); Redis Streams orchestration with parallelism and reopen-on-reject (TITAN-FND-009–011); Self-Improvement Loop minus Security/Perf stages (TITAN-FND-013); git workspaces and local deploy of generated apps (TITAN-FND-012, -027); LLM Gateway with all four providers, per-role routing, cost tracking (TITAN-FND-019–021); project memory + Qdrant retrieval (TITAN-FND-016–017); dashboard subset with WebSocket streaming (TITAN-FND-022–023); local auth and approval gates (TITAN-FND-028–029); observability stack in Compose (TITAN-FND-061).

**Exit criteria:** MVP KPI column met; the brief "build a task-tracking web app" completes autonomously on both cloud and local profiles.

### 9.2 v1 — "A Real Organization" (M2)

Full 15–20 agent roster incl. Security, DevOps, Database, Documentation, Research, Memory (Vol. 2); Security Scan + Performance Analysis stages and Security release-blocking (TITAN-FND-013, -015); organizational memory and cited knowledge base (TITAN-FND-016, -018); full dashboard incl. Architecture Viewer, Memory Explorer, Deployment Dashboard, Monitoring, Plugin Manager (TITAN-FND-022); plugin architecture GA with GitHub and Slack plugins (TITAN-FND-025–026); OIDC, RBAC, budget auto-pause (TITAN-FND-024, -029–030); Kubernetes/Helm production deployment (Vol. 9) with NATS JetStream tested as queue scale-out (ADR-005); complete auto-generated artifacts (TITAN-FND-004).

### 9.3 v2 — "The Platform Learns" (M3)

Cross-project learning and reusable-pattern mining (Vols. 5, 10); agent self-evaluation, reflection loops, AI debates, multi-model collaboration (Vol. 10); plugin marketplace and custom user-defined agents (Vol. 10); cloud deployment plugins for generated apps (TITAN-FND-027 MAY scope); enterprise hardening — ASVS L2, air-gap profile, data residency (Vol. 8); v2 KPI column incl. ≤ 30 min time-to-first-deploy.

---

## 10. High-Level System Architecture

TITAN is a **modular monolith at MVP** — one FastAPI process hosting API, orchestrator, agent runtime, and gateway as boundary-enforced packages — decomposable into services at scale; the module seams are the service seams (Vol. 3). This is the canonical context view:

```
                          ┌───────────────────────────────────────────────┐
                          │              USER (the client)                │
                          └──────────────┬───────────────┬────────────────┘
                                         │ HTTPS         │ WebSocket
                          ┌──────────────▼───────────────▼────────────────┐
                          │   FRONTEND — Next.js 15 / React / TypeScript  │
                          │   Tailwind CSS / shadcn/ui  (dashboard,       │
                          │   Kanban, live conversations, memory, ...)    │
                          └──────────────┬───────────────┬────────────────┘
                                         │ REST /api/v1  │ WS /ws
┌────────────────────────────────────────▼───────────────▼────────────────────────────┐
│                 BACKEND / ORCHESTRATOR — Python 3.12 + FastAPI (async)               │
│                                                                                      │
│  ┌──────────────┐  ┌────────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  API Layer   │  │  Orchestrator      │  │  Agent Runtime   │  │  LLM Gateway  │  │
│  │  REST + WS   │  │  task DAG,         │  │  15–20 agents:   │  │  Anthropic /  │  │
│  │  OAuth2/OIDC │  │  scheduler,        │  │  CEO, PM, Arch,  │  │  OpenAI /     │  │
│  │  JWT, RBAC   │  │  Self-Improvement  │  │  Eng, QA, Sec,   │  │  Google /     │  │
│  │              │  │  Loop state machine│  │  Reviewer, ...   │  │  Ollama local │  │
│  └──────┬───────┘  └─────────┬──────────┘  └────────┬─────────┘  └───────┬───────┘  │
│         │                    │                      │                    │          │
│  ┌──────▼────────────────────▼──────────────────────▼────────────────────▼───────┐  │
│  │     Plugin & Tool Bus  (git, shell, docker, GitHub, Slack, MCP, ...)          │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
└───────┬───────────────────┬────────────────────┬─────────────────────┬──────────────┘
        │ SQL               │ streams/cache      │ vectors             │ files
┌───────▼────────┐  ┌───────▼────────┐  ┌────────▼───────┐  ┌──────────▼───────────┐
│ PostgreSQL 16  │  │    Redis 7     │  │     Qdrant     │  │  Project Workspaces  │
│ system of      │  │ Streams queue, │  │ vector memory, │  │  git repos of        │
│ record: msgs,  │  │ cache, pub/sub │  │ semantic       │  │  generated apps,     │
│ tasks, ADRs,   │  │ (NATS JetStream│  │ retrieval      │  │  built & deployed    │
│ audit, users   │  │ at scale-out)  │  │                │  │  via Docker          │
└────────────────┘  └────────────────┘  └────────────────┘  └──────────────────────┘
        └───────────────────┴────────────────────┴─────────────────────┘
                                        │ telemetry (OpenTelemetry)
                     ┌──────────────────▼───────────────────────┐
                     │ OBSERVABILITY: OTel Collector →           │
                     │ Prometheus · Loki · Grafana · traces      │
                     └───────────────────────────────────────────┘

  Local: everything above via Docker Compose on one laptop.
  Prod:  same containers on Kubernetes (Helm), HPA on agent workers.
```

Canonical dataflow (detailed in Vol. 6): brief → API → CEO/PM/Architect plan → orchestrator materializes the task DAG in PostgreSQL and enqueues ready tasks on Redis Streams → agent workers consume tasks, call models through the LLM Gateway, act on workspaces via the Tool Bus → every message and state change persists to PostgreSQL and streams to the UI over WebSockets → the Self-Improvement Loop gates every artifact before merge → approved releases deploy via Docker.

---

## 11. Technology Stack Selection and Justification

This volume **owns** stack justification; other volumes cite these ADRs (e.g., "per ADR-005") without repeating the argument. Changing any decision requires amending this section (C-5).

### 11.1 Canonical Stack and Trade-off Table

| Layer | Canonical Choice | Leading Alternatives | Decisive Trade-off |
|---|---|---|---|
| Repo layout | Monorepo | Polyrepo | Cross-cutting agent changes and shared types outweigh repo-size cost |
| Frontend | Next.js 15 + React + TypeScript + Tailwind CSS + shadcn/ui | SvelteKit; Vue/Nuxt; Vite SPA | Ecosystem depth; streaming UI; shadcn/ui is owned code, not a dependency |
| Backend | Python 3.12 + FastAPI (async) + SQLAlchemy + Pydantic v2 | NestJS; Go | AI/LLM ecosystem gravity; Pydantic as message-schema backbone |
| System of record | PostgreSQL 16 | MySQL; MongoDB | ACID + JSONB covers relational and document needs; boring and free |
| Cache/queue/pub-sub | Redis 7 (Streams) | RabbitMQ; Kafka; NATS | One container serves three roles → smallest local footprint |
| Queue at scale | NATS JetStream (documented option) | Kafka | Far lighter operationally; semantics map from Streams |
| Vector DB | Qdrant | pgvector; Weaviate; Chroma; Pinecone | OSS, small container, first-class filtering; Pinecone fails C-1 |
| Realtime | WebSockets via FastAPI | SSE; socket.io; Pusher | Duplex needed (approvals travel up); no extra service; Pusher fails C-1 |
| LLM access | Provider-agnostic gateway (Anthropic, OpenAI, Google, Ollama) | Direct SDKs; LangChain coupling | Config-only switching is a product requirement (TITAN-FND-019/020) |
| Local runtime | Docker Compose | Nix; bare venv/node | One-command onboarding; same containers promote to K8s |
| Production | Kubernetes (Helm) | ECS; Nomad; Fly.io | Cloud-neutral (TITAN-FND-063); HPA on workers |
| Observability | OpenTelemetry + Prometheus + Grafana + Loki | Datadog; ELK | Fully OSS; OTel keeps instrumentation vendor-neutral |
| Auth | OAuth2/OIDC + JWT + RBAC | Sessions only; Auth0/Clerk | OIDC = enterprise SSO path; JWT suits WS auth; local mode needs no IdP |

### 11.2 Decision Records

**ADR-001 — Monorepo**
- **Context:** TITAN spans a TypeScript frontend, Python backend, shared schemas, infra, and docs — edited by humans and by TITAN's own agents, whose changes are frequently cross-cutting.
- **Decision:** One monorepo (`apps/web`, `apps/api`, `packages/schemas`, `infra/`, `docs/`); TypeScript types generated from Pydantic/OpenAPI bridge the languages.
- **Alternatives:** Polyrepo — rejected: cross-repo atomic changes and version skew are poison for agents that must see the whole system. Bazel-style build tooling — rejected: onboarding cost violates the ≤ 15 min setup NFR.
- **Trade-offs:** Accept larger clones and mixed-language CI; gain atomic cross-stack commits, one source of truth for agent code-reading, and simpler onboarding.

**ADR-002 — Python 3.12 + FastAPI (async) + SQLAlchemy + Pydantic v2**
- **Context:** The backend is I/O-bound orchestration (LLM calls, queue, DB) at the center of a Python-first AI ecosystem.
- **Decision:** Python 3.12, fully async FastAPI, SQLAlchemy 2.x async, Pydantic v2 for all models — including the typed agent messages of TITAN-FND-006.
- **Alternatives:** NestJS/TypeScript (one language across the stack) — rejected: the LLM tooling ecosystem, from provider SDKs to tokenizers to local-model glue, is Python-first; fighting that daily costs more than a two-language stack. Go — rejected: weakest LLM ecosystem, slower iteration on agent logic.
- **Trade-offs:** Accept Python's per-process ceiling and the GIL (mitigated: async I/O-bound workload, horizontal workers); gain Pydantic v2 (Rust-core) validation as the message backbone, first-class SDKs, and the largest OSS-AI contributor pool.

**ADR-003 — Next.js 15 + React + TypeScript + Tailwind CSS + shadcn/ui**
- **Context:** The dashboard is large (12+ screen areas), real-time, dark-mode-first, and maintained by contributors and by TITAN's own Frontend Agent.
- **Decision:** Next.js 15 (App Router, RSC), strict TypeScript, Tailwind, shadcn/ui.
- **Alternatives:** SvelteKit — rejected: far smaller component/contributor ecosystem, and LLMs generate React most reliably, which matters when agents maintain the UI. Vite SPA — rejected: loses streaming/RSC and routing conventions that keep a 12-screen app coherent. MUI/Chakra — rejected: shadcn/ui vendors components into the repo, giving full theming control and no upstream breakage, ideal for agent modification.
- **Trade-offs:** Accept Next.js framework complexity; gain conventions that constrain generated code, streaming for live agent feeds, and accessible Radix primitives supporting the a11y AC of TITAN-FND-022.

**ADR-004 — PostgreSQL 16 as the single system of record**
- **Context:** Durable, audit-critical, strongly consistent state: projects, task DAGs, every agent message (TITAN-FND-007), decisions, users, audit logs (TITAN-FND-052).
- **Decision:** PostgreSQL 16 for all durable state — relational tables for tasks/projects/users, JSONB for message payloads, LISTEN/NOTIFY available for change signals.
- **Alternatives:** MongoDB — rejected: the core data is relational (DAG edges, joins for Kanban) and transactional integrity is the crash-recovery backbone. MySQL — rejected: weaker JSONB/extension story. Separate event store — rejected at MVP: a second system without need; event sourcing stays a Vol. 3 option inside Postgres.
- **Trade-offs:** Accept a single engine to scale later (replicas/partitioning); gain ACID everywhere, one backup story, and a 256 MB local footprint.

**ADR-005 — Redis 7 (Streams) at MVP; NATS JetStream as scale-out option**
- **Context:** The task/agent queue needs consumer groups, redelivery, and ordering (TITAN-FND-011); the platform also needs a cache and pub/sub for WebSocket fan-out; C-1 demands a minimal footprint.
- **Decision:** Redis 7 serves all three roles at MVP — Streams + consumer groups for the queue, key/value cache, pub/sub for realtime fan-out — behind a `TaskQueue` interface. **NATS JetStream** is the canonical scale-out replacement, documented and integration-tested at v1.
- **Alternatives:** Kafka — rejected for MVP: disproportionate footprint and ops burden on a laptop. RabbitMQ — rejected: adds a fourth service while Redis is already present. NATS from day one — rejected: Redis is still needed for caching, so MVP would run two systems where one suffices.
- **Trade-offs:** Accept weaker delivery guarantees than a dedicated broker (mitigated by idempotent handlers and PostgreSQL as source of truth) and a bounded future migration; gain one ~30 MB container covering three needs — the largest single contributor to the 16 GB local budget.

**ADR-006 — Qdrant as the vector database**
- **Context:** Memory and RAG (TITAN-FND-016/017, Vol. 5) need semantic search over messages, code, decisions, and docs with per-project/type metadata filtering, locally and free.
- **Decision:** Qdrant, single container, with a swappable embedding model — local sentence-transformers by default, provider embeddings optional via the gateway.
- **Alternatives:** pgvector — rejected as canonical: weaker filtering/ANN operations at organizational-memory scale, and coupling vector load to the system of record threatens the p95 read NFR; retained as the documented "minimal profile" fallback. Chroma — rejected: less mature for concurrent multi-project use. Weaviate — rejected: heavier, more opinionated. Pinecone — rejected outright: paid SaaS violates C-1.
- **Trade-offs:** Accept one more container (~150 MB); gain purpose-built ANN performance and the rich payload filtering Vol. 5's retrieval pipeline depends on.

**ADR-007 — Provider-agnostic LLM Gateway (Anthropic, OpenAI, Google, Ollama)**
- **Context:** Config-only provider switching (TITAN-FND-019/020) serves cost (Dana), governance (Meridian), and zero-budget use (Priya); per-role model routing and cost metering (TITAN-FND-021) are product features.
- **Decision:** A single in-process gateway module (extractable to a service later): unified chat/tool-call/embedding interface, four provider adapters, per-role routing table in config, retry/failover (TITAN-FND-053), token/cost accounting, full OTel tracing. Agents never import provider SDKs directly — enforced by lint rule.
- **Alternatives:** Direct SDK use per agent — rejected: N×M coupling kills config-only switching. LangChain/LiteLLM as the abstraction — LiteLLM may be used *inside* an adapter, but TITAN's own interface is the contract; external abstractions churn too fast to anchor a spec.
- **Trade-offs:** Accept adapter maintenance and a common-denominator interface (provider extras exposed via typed capability flags); gain the product requirement itself — provider swaps in one YAML edit, uniform budget caps, one egress audit point.

**ADR-008 — Docker Compose local; Kubernetes (Helm) production**
- **Context:** C-1 requires the whole platform on one laptop in ≤ 15 minutes; enterprises require declarative, scalable production (TITAN-FND-051).
- **Decision:** One `docker-compose.yml` runs web, api, workers, PostgreSQL, Redis, Qdrant, and observability locally. Production ships as Helm charts deploying the *same images* to any Kubernetes, HPA on agent workers.
- **Alternatives:** Nix — rejected: contributor onboarding cost. Fly.io/ECS as primary target — rejected: vendor coupling violates TITAN-FND-063. k3d/kind locally — optional profile for chart testing, not the default.
- **Trade-offs:** Accept two deployment descriptors (mitigated: same images, env-based config in both); gain dev/prod parity and a zero-cloud local story.

**ADR-009 — WebSockets served by FastAPI**
- **Context:** The dashboard streams agent messages, task transitions, and logs live (TITAN-FND-023) and carries approvals back.
- **Decision:** Native FastAPI WebSocket endpoints, JWT-authenticated, fanned out across instances via Redis pub/sub.
- **Alternatives:** SSE — rejected: uni-directional; approvals and agent chat need duplex. socket.io — rejected: protocol lock-in on both ends for unneeded features. Pusher/Ably — rejected: violates C-1.
- **Trade-offs:** Accept in-house reconnect/connection-state logic (specified in Vol. 4); gain a duplex channel with no extra infrastructure and one auth model.

**ADR-010 — OAuth2/OIDC + JWT with RBAC**
- **Context:** Enterprises need SSO (Meridian); teams need roles (US-5); Priya needs zero external identity dependency (C-1).
- **Decision:** OAuth2/OIDC (any compliant IdP; Keycloak as OSS reference), JWT access tokens for API and WebSocket auth, RBAC with Owner/Maintainer/Viewer as the base set (extended in Vol. 8). Local single-user mode issues JWTs from a built-in bootstrap identity — no IdP required.
- **Alternatives:** Server-side sessions only — rejected: awkward for WS, API tokens, and horizontal scale. Auth0/Clerk — rejected: C-1. Fully custom auth — rejected: security surface without benefit; OIDC is what enterprises demand.
- **Trade-offs:** Accept JWT revocation discipline (short TTL + refresh, Vol. 8); gain standard SSO, stateless service auth, identical mechanism locally and in production.

**ADR-011 — OpenTelemetry + Prometheus + Grafana + Loki**
- **Context:** A system that acts unattended for hours is undebuggable without correlated traces; TITAN-FND-061 demands one distributed trace per agent task, on free tooling.
- **Decision:** OTel SDKs in api/workers/gateway → OTel Collector; Prometheus metrics, Loki logs, Grafana dashboards — preconfigured in Compose and Helm.
- **Alternatives:** Datadog/New Relic — rejected: paid, C-1. ELK — rejected: Elasticsearch's RAM appetite breaks the 16 GB budget; Loki's label model is cheaper and sufficient. "Just logs" — rejected: cross-service agent workflows require traces.
- **Trade-offs:** Accept ~4 observability containers locally (~1.5 GB; a `--profile minimal` Compose profile can disable them on constrained machines); gain vendor-neutral instrumentation and out-of-the-box "follow one task through the system."

---

## 12. Development Philosophy and Engineering Principles

These principles bind human contributors, TITAN's agents when they modify TITAN, and the code TITAN generates (elaborated as the engineering "constitution" in the Engineering Standards volume).

1. **Build an organization, not a monolith brain.** Capability comes from specialized roles, structured communication, and adversarial review — not one ever-larger prompt.
2. **Never accept the first draft.** Every artifact passes the Self-Improvement Loop; a 0% rejection rate is a defect in the reviewers, not excellence in the authors (see the KPI band).
3. **The human is the client; consent is sacred.** Autonomy operates between explicit, configurable approval gates (TITAN-FND-028) — few questions, but gates are never bypassed.
4. **Local-first is a feature, not a fallback.** Every capability ships with a free/local profile. If it cannot run on Priya's laptop, it is not core.
5. **Everything persists; nothing is lost.** Messages, decisions, critiques, and costs are durable records (TITAN-FND-007); debuggability and organizational learning both depend on total recall.
6. **Boring technology, exciting architecture.** Postgres, Redis, and Docker are deliberately unexciting; the novelty budget is spent on agent intelligence.
7. **Interfaces over implementations.** Queue, vector store, LLM provider, and plugins sit behind typed interfaces (ADR-005/006/007, TITAN-FND-025) so components swap without core changes.
8. **Strict typing end to end.** Pydantic v2 and strict TypeScript are the contract layer; `mypy --strict` and `tsc --strict` gate CI (TITAN-FND-064); untyped agent messages are rejected at runtime (TITAN-FND-006).
9. **No placeholders, no TODOs.** In specs, platform code, and generated code, every phase completes and is reviewed before the next begins — per the source mandate's 13-phase process.
10. **Observable by default.** New code paths must be traceable end-to-end before merge (TITAN-FND-061). SOLID, Clean Architecture, DI, and the repository pattern apply where they serve these goals; DDD and CQRS only where an ADR justifies them (Vol. 3).
11. **Measure the product, not the vibes.** Every release is judged against Section 7's KPI table on the fixed benchmark-brief suite.

---

## 13. Risks and Open Questions

### 13.1 Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Small local models (7–13B) cannot complete complex briefs → local-only KPI missed | High | High | Decomposition tuned for small contexts (Vol. 5); staged local-only KPI (3/10 → 8/10); per-role routing lets a strong cloud model backstop hard tasks where allowed |
| R2 | Review ping-pong burns tokens without converging | Medium | High | Loop iteration caps, budget caps (TITAN-FND-024), escalation to human after N rejections (Vol. 6) |
| R3 | Redis Streams delivery edge cases corrupt orchestration state | Low | High | PostgreSQL as sole source of truth; idempotent handlers; chaos suite in CI (TITAN-FND-052) |
| R4 | Provider API churn breaks gateway adapters | Medium | Medium | Nightly contract tests against live + recorded fixtures; gateway interface limits blast radius |
| R5 | Ten parallel volumes drift or contradict | Medium | High | Canonical decisions + C-5 amendment process; cross-reference review before each volume's v1.0 |
| R6 | Agent-executed shell/docker tools endanger user machines | Medium | Critical | Sandboxed (containerized) tool runs, allowlisted commands, approval gates for destructive actions (Vol. 8) |
| R7 | OSS project fails to attract maintainers | Medium | Medium | ≤ 15 min setup NFR, spec-first docs, benchmark suite as a contribution target |

### 13.2 Open Questions (tracked; none block MVP implementation)

| # | Question | Owner | Resolve by |
|---|---|---|---|
| Q1 | Final OSS licence: Apache-2.0 (working assumption series-wide) vs. AGPL-3.0 to deter closed SaaS forks | Vol. 1 amendment | First public release |
| Q2 | Default local embedding model (quality vs. RAM on 16 GB machines) | Vol. 5 | MVP feature freeze |
| Q3 | LLM Gateway extracted to a standalone service at v1, or in-process until v2 | Vol. 3 | v1 architecture review |
| Q4 | Composition of the fixed 10-brief benchmark suite behind Section 7 KPIs | Vol. 6 | MVP test-plan sign-off |
| Q5 | Agents as RBAC principals with scoped permissions: v1 or v2 | Vol. 8 | v1 security review |

---

## 14. Cross-References

| Volume | Title | What it takes from Volume 1 |
|---|---|---|
| Vol. 2 | Multi-Agent Intelligence | Agent roster (TITAN-FND-005), communication and authority model (TITAN-FND-006–008) |
| Vol. 3 | Backend Engineering & Infrastructure | Architecture (Sec. 10); ADR-002/004/005/008/009/010; NFR targets (Sec. 5); Q3 |
| Vol. 4 | Frontend & Dashboard | Screen inventory (TITAN-FND-022–024); ADR-003/009; personas driving UX |
| Vol. 5 | AI Brain — Memory, Knowledge & RAG | Memory requirements (TITAN-FND-016–018); ADR-006/007; R1, Q2 |
| Vol. 6 | Autonomous Engineering Workflow | Self-Improvement Loop (TITAN-FND-013); orchestration (TITAN-FND-009–011); gates (TITAN-FND-028); Q4 |
| Vol. 7 | Tool Ecosystem & Integrations | Plugin architecture (TITAN-FND-025–027); Tool Bus (Sec. 10); C-6 |
| Vol. 8 | Security & Compliance | TITAN-FND-029/030/062; ADR-010; R6, Q5 |
| Vol. 9 | Deployment & Production Operations | ADR-008/011; TITAN-FND-051/052/061; roadmap milestones |
| Vol. 10 | Autonomous Evolution & Future | v2 roadmap (Sec. 9.3); KPI evolution targets; marketplace scope |

---

## 15. Glossary of TITAN Terms

| Term | Definition |
|---|---|
| **Agent** | A specialized autonomous worker (e.g., Backend Engineer Agent) with a defined role, tools, memory, decision authority, and message interfaces (Vol. 2). |
| **Agent Runtime** | The backend subsystem that instantiates agents, feeds them tasks, and mediates tool/LLM access. |
| **Approval Gate** | A configurable checkpoint where autonomous execution pauses for human consent (TITAN-FND-028). |
| **Benchmark-Brief Suite** | The fixed set of 10 project briefs used to measure KPIs across releases (Q4). |
| **Brief** | The user's natural-language project request that initiates a Project. |
| **Clarification Round** | The bounded set of essential questions (≤ 7 at MVP) asked after intake (TITAN-FND-002). |
| **Decision Log** | Persisted ADR-style records of significant choices, citable by ID. |
| **Knowledge Base** | Curated organizational content — standards, patterns, templates, references — that agents cite (TITAN-FND-018). |
| **LLM Gateway** | The provider-agnostic model-access layer (ADR-007); the only path to any LLM. |
| **Local-Only Profile** | Configuration in which every component, including models (Ollama), runs on the user's machine with zero paid calls. |
| **Memory (short-term / project / organizational)** | In-context working state; durable knowledge scoped to one project; durable knowledge shared across all projects (Vol. 5). |
| **Message (structured)** | A Pydantic-validated inter-agent communication: Task Assignment, Progress Report, Review Request, Architecture Discussion, Bug Report, Decision Log, Meeting Summary, or Conflict Resolution. |
| **Orchestrator** | The scheduler that materializes task DAGs, enqueues ready tasks, enforces the Self-Improvement Loop state machine, and handles escalation. |
| **Plugin / Tool Bus** | The extension architecture through which integrations and agent tools register without core changes (TITAN-FND-025, Vol. 7). |
| **Project Workspace** | The per-project git repository holding all generated artifacts, built and deployed via Docker. |
| **Reference Hardware** | The 4-core/16 GB laptop and 3-node cluster against which all NFRs are measured (C-3). |
| **Self-Improvement Loop** | The mandatory pipeline for every artifact: Implementation → Review → Critique → Refactor → Testing → Security Scan → Performance Analysis → Approval → Merge (TITAN-FND-013). |
| **Task DAG** | The dependency graph of tasks derived from a Brief; must be acyclic (TITAN-FND-003). |
| **TITAN** | This platform: the open-source, local-first, multi-agent autonomous software engineering system specified by this 10-volume series. |

---

*End of Volume 1 — Foundation & Product Requirements (TITAN-VOL-01, Draft v1.0, 2026-07-04).*
