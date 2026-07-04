# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 7 — Integrations, Plugin System & Tool Ecosystem

| Field | Value |
|---|---|
| **Series** | PROJECT TITAN — Autonomous Software Engineering Platform (10 volumes) |
| **Volume** | 7 — Integrations, Plugin System & Tool Ecosystem |
| **Doc ID** | TITAN-VOL-07 |
| **Status** | Draft v1.0 |
| **Date** | 2026-07-04 |
| **Requirement prefix** | TITAN-INT-NNN |

**Abstract.** This volume specifies how TITAN's agents reach the outside world: the plugin architecture that lets third parties add capabilities without touching core code, the tool abstraction layer through which every agent action is declared, sandboxed, and audited, the core tool set (Git, filesystem, terminal, Docker, browser, web research), the concrete integration specifications for GitHub, GitLab, Slack, Discord, Jira, Linear, Figma, VS Code, and cloud providers, first-class support for the Model Context Protocol (MCP) as a plugin transport, and the multi-LLM provider gateway that makes Anthropic, OpenAI, Google, and local Ollama models interchangeable through configuration alone. It is written to be implemented directly: schemas, lifecycle state machines, permission scopes, and wire formats are normative.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Ecosystem Architecture Overview](#2-ecosystem-architecture-overview)
3. [Plugin System](#3-plugin-system)
4. [Tool Abstraction Layer](#4-tool-abstraction-layer)
5. [Core Tool Set](#5-core-tool-set)
6. [External Integration Specifications](#6-external-integration-specifications)
7. [MCP — Model Context Protocol Support](#7-mcp--model-context-protocol-support)
8. [Multi-LLM Provider Gateway](#8-multi-llm-provider-gateway)
9. [Decision Records](#9-decision-records)
10. [Risks & Open Questions](#10-risks--open-questions)
11. [Cross-References](#11-cross-references)

---

## 1. Introduction

### 1.1 Purpose

TITAN's agents (Volume 2) are only as capable as the tools they can call. This volume defines the three layers that turn a reasoning loop into an engineering organization that can ship software: (a) a **plugin system** so new integrations require zero core changes, (b) a **tool abstraction layer** that gives every agent a uniform, sandboxed, audited way to act, and (c) a **provider gateway** that decouples agents from any single LLM vendor.

### 1.2 Scope

In scope: plugin manifest, lifecycle, permissions, and versioning; tool registry and JSON-Schema tool definitions; execution sandboxing and audit logging; core tools (Git/GitHub/GitLab, filesystem, terminal, Docker, browser automation, web research); integration specs for GitHub, Slack/Discord, Jira/Linear, Figma, VS Code, and cloud providers; MCP as a first-class transport; the multi-LLM gateway with per-agent routing, fallbacks, and cost accounting.

Out of scope: agent role definitions (Volume 2), memory/RAG internals (Volume 5), workflow orchestration (Volume 6), platform-wide security policy (Volume 8), and production deployment topology (Volume 9). Canonical stack decisions (Next.js 15 frontend; Python 3.12 + FastAPI backend; PostgreSQL 16 / Redis 7 / Qdrant; Redis Streams; OAuth2/OIDC + JWT + RBAC; Docker Compose locally, Kubernetes in production) are owned and justified by Volume 1 and are treated here as fixed.

### 1.3 Terminology

| Term | Definition |
|---|---|
| **Tool** | A named, JSON-Schema-typed capability an agent can invoke (e.g. `git.commit`). |
| **Plugin** | A distributable package that registers tools, agents, UI panels, webhooks, or providers. |
| **Tool Host** | The sandboxed process pool that executes tool calls. |
| **Connector** | A plugin whose primary purpose is bridging an external SaaS (GitHub, Jira, …). |
| **Gateway** | The provider-agnostic LLM access layer. |
| **MCP** | Model Context Protocol — an open protocol for exposing tools/resources to LLM apps. |

Priorities follow RFC 2119: MUST (mandatory for MVP), SHOULD (expected, deferrable with an ADR), MAY (optional).

---

## 2. Ecosystem Architecture Overview

All agent side-effects flow through one chokepoint — the Tool Bus — regardless of whether the capability is built-in, a Python plugin, or a remote MCP server. Similarly, all LLM traffic flows through the Gateway. Nothing else in TITAN talks to the network on an agent's behalf.

```
                 +--------------------------------------------------------+
                 |                TITAN Orchestrator (FastAPI)             |
                 |                                                        |
  +---------+   |  +-----------+      +---------------------------+      |
  | Agents  |---+->| Tool Bus  |----->|       Tool Registry       |      |
  | (Vol 2) |   |  | (validate,|      | built-in | plugin | MCP   |      |
  +---------+   |  |  authz,   |      +-----+---------+------+----+      |
       |        |  |  audit)   |            |         |      |           |
       |        |  +-----------+            v         v      v           |
       |        |                    +-----------+ +------+ +---------+  |
       v        |                    | Tool Host | |Plugin| | MCP     |  |
  +---------+   |                    | (sandbox  | | Rt.  | | Client  |  |
  | LLM     |   |                    |  workers) | +------+ +----+----+  |
  | Gateway |   |                    +-----+-----+              |        |
  +----+----+   +--------------------------|--------------------|--------+
       |                                   v                    v
       v                        +--------------------+   +--------------+
 Anthropic / OpenAI /           | Git, FS, Terminal, |   | MCP servers  |
 Google / Ollama                | Docker, Browser    |   | (stdio/HTTP) |
                                +--------------------+   +--------------+
                                           |
                                           v
                          GitHub / GitLab / Slack / Jira / Linear /
                          Figma / Cloud APIs (via connector plugins)
```

**TITAN-INT-001 (MUST)** — Every externally visible side-effect performed by an agent MUST pass through the Tool Bus; no agent code path may open sockets, spawn processes, or write files directly. *Acceptance:* code review gate + a CI lint rule rejecting `subprocess`, `open()` (write mode), and raw HTTP clients inside `titan/agents/**`; runtime egress policy blocks agent workers from all hosts except the orchestrator.

**TITAN-INT-002 (MUST)** — Every LLM call MUST pass through the Provider Gateway. *Acceptance:* no provider SDK is imported outside `titan/gateway/**`; integration test proves an agent run succeeds with only gateway credentials configured.

---

## 3. Plugin System

### 3.1 Design principles

1. **Core stays small.** GitHub, Slack, Jira, etc. are plugins shipped in the monorepo (`plugins/first_party/*`) using the same API as third-party plugins — first-party code gets no private hooks.
2. **Declarative surface, imperative core.** A plugin declares *what* it contributes in a manifest; the runtime decides *where and how* it loads.
3. **Deny-by-default permissions.** A plugin can do nothing it did not declare and the operator did not approve.
4. **Local-first.** Plugins install from a local path or Git URL with no central marketplace required (marketplace is Volume 10 scope).

### 3.2 Plugin manifest format

Each plugin is a directory (or wheel/archive) containing `titan-plugin.yaml` at its root. The manifest schema is published as JSON Schema at `schemas/plugin-manifest.v1.json` and validated with Pydantic v2 on install.

**Example — the first-party Jira connector manifest:**

```yaml
manifest_version: 1
id: titan.jira                 # reverse-DNS, globally unique
name: Jira Sync
version: 1.2.0                 # SemVer, required
description: Two-way sync of TITAN tasks with Jira issues.
author: TITAN Core Team
license: Apache-2.0
homepage: https://github.com/titan-platform/titan
titan_api: ">=1.0,<2.0"        # core plugin-API compatibility range

runtime:
  type: python                 # python | mcp
  entrypoint: titan_jira.plugin:JiraPlugin   # subclass of titan.plugins.Plugin
  python: ">=3.12"

permissions:
  network:
    - "https://*.atlassian.net"   # explicit egress allowlist
  secrets:
    - JIRA_API_TOKEN               # names resolved from the secret store
    - JIRA_EMAIL
  events:
    subscribe: [task.created, task.status_changed]
    publish:   [integration.jira.synced]
  scopes: [tasks:read, tasks:write, projects:read]   # TITAN RBAC scopes

contributes:
  tools:
    - name: jira.create_issue
      schema: schemas/create_issue.json
      risk: write                # read | write | admin (see 4.4)
    - name: jira.transition_issue
      schema: schemas/transition_issue.json
      risk: write
  agents: []                     # may contribute agent role definitions (Vol 2 format)
  ui_panels:
    - id: jira-board-link
      title: Jira
      mount: project.sidebar     # named extension slot in the dashboard (Vol 4)
      type: iframe               # iframe | schema-form | markdown
      src: /plugin-ui/titan.jira/panel.html
  webhooks:
    - path: /hooks/jira          # mounted at /api/v1/plugins/titan.jira/hooks/jira
      verify: hmac-sha256
      secret: JIRA_WEBHOOK_SECRET

config_schema: schemas/config.json   # JSON Schema rendered as a settings form
```

**TITAN-INT-010 (MUST)** — Plugins MUST ship a `titan-plugin.yaml` conforming to `plugin-manifest.v1`; installation MUST fail with actionable errors on schema violation, unknown permission scope, or `titan_api` incompatibility. *Acceptance:* installer test suite covers each rejection class; error message names the offending field.

**TITAN-INT-011 (MUST)** — A plugin MUST be able to contribute tools, agent definitions, UI panels, webhook endpoints, and event subscriptions purely via its manifest and entrypoint, with zero modifications to core packages. *Acceptance:* the first-party GitHub, Slack, and Jira connectors are implemented as plugins; deleting any of their directories leaves core tests green.

**TITAN-INT-012 (SHOULD)** — Manifests SHOULD be signed (detached Ed25519 signature, `titan-plugin.yaml.sig`); unsigned plugins install only when the operator sets `plugins.allow_unsigned: true`. *Acceptance:* tampered manifest fails verification; unsigned install emits a persistent warning banner in the Plugin Manager UI.

### 3.3 Plugin lifecycle

```
             +----------+   install    +-----------+  enable   +---------+
  discover ->| AVAILABLE |------------>| INSTALLED |---------->| ENABLED |
             +----------+  (validate,  +-----------+ (grant    +----+----+
                            resolve      |  ^        perms,         |
                            deps, pin    |  | disable sandbox       | error x3 /
                            version)     |  +---------------+       | healthcheck fail
                                         v                  |       v
                                    +---------+         +---+-----+---+
                                    | REMOVED |<--------|  QUARANTINED |
                                    +---------+ uninstall+-------------+
```

Lifecycle stages, each an explicit REST operation on `/api/v1/plugins` and a Plugin Manager UI action (Volume 4):

1. **Discover** — sources: local directory scan (`./plugins`), Git URL, OCI artifact reference. The registry records origin and content hash.
2. **Install** — validate manifest, resolve Python deps into an isolated virtualenv per plugin (never the core environment), persist a `plugin_installs` row (PostgreSQL) with pinned version + hash.
3. **Enable** — operator (RBAC role `plugin:admin`) reviews and grants the declared permissions; runtime loads the entrypoint inside its sandbox; healthcheck (`Plugin.health()`) must return OK within 10 s.
4. **Configure** — settings form generated from `config_schema`; values stored encrypted (Volume 8); secrets referenced by name only, injected at runtime.
5. **Sandbox (continuous)** — see §3.5; violations move the plugin to QUARANTINED.
6. **Disable / Uninstall** — disable revokes permissions and unloads tools atomically (in-flight tool calls drain, new calls rejected with `TOOL_UNAVAILABLE`); uninstall additionally deletes the virtualenv and config, retaining audit rows.

**TITAN-INT-013 (MUST)** — Lifecycle transitions MUST be atomic and crash-safe: a failure mid-install/enable leaves the previous state intact. *Acceptance:* chaos test kills the orchestrator during each transition; on restart the registry state matches the last committed transition.

**TITAN-INT-014 (MUST)** — Three consecutive unhandled exceptions or a failed healthcheck MUST quarantine a plugin automatically, unregistering its tools and notifying operators via the notification bus. *Acceptance:* fault-injection test observes quarantine within 30 s and a `plugin.quarantined` event on Redis Streams.

### 3.4 Permission model

Plugin permissions are capability grants, checked at two layers: **grant time** (operator approves the manifest's requested set; any later manifest expansion re-triggers approval) and **call time** (Tool Bus verifies the *calling agent's* RBAC scopes intersect the tool's required scopes — a plugin cannot launder an agent into privileges the agent lacks).

| Permission class | Granularity | Enforced by |
|---|---|---|
| `network` | URL-pattern egress allowlist | Sandbox egress proxy |
| `secrets` | Named secrets only | Secret store injector |
| `scopes` | TITAN RBAC scopes (`tasks:write`, …) | Tool Bus authz |
| `events` | Explicit subscribe/publish topics | Event bus ACL |
| `filesystem` | Workspace-relative path globs | Tool Host mount policy |

**TITAN-INT-015 (MUST)** — Permissions are deny-by-default; a plugin call touching an undeclared or ungranted capability MUST fail closed and be audit-logged as a `PERMISSION_DENIED` event. *Acceptance:* conformance suite attempts one violation per class; all five are blocked and logged.

### 3.5 Sandboxing of plugin code

Python plugins run **out-of-process** in a per-plugin worker (local dev: subprocess with seccomp-lite via `python -I`, resource limits via cgroups where available; production: dedicated container with read-only rootfs, non-root UID, egress restricted to the manifest allowlist by a sidecar proxy; Kubernetes NetworkPolicy mirrors the same allowlist). Communication with core is JSON-RPC 2.0 over stdio — deliberately identical in shape to MCP (§7), so the plugin runtime and MCP client share one codebase.

**TITAN-INT-016 (MUST)** — Plugin code MUST NOT share a process with the orchestrator; a crashing or hanging plugin MUST NOT take down core (call timeout default 60 s, configurable per tool). *Acceptance:* a plugin executing `os._exit(1)` and one sleeping forever both leave the orchestrator serving requests; the offending calls return structured errors.

### 3.6 Versioning and compatibility

- Plugins use **SemVer**; the core plugin API is versioned independently (`titan_api`, currently `1.x`).
- Core guarantees backward compatibility within a major `titan_api` version; deprecations emit warnings for ≥1 minor release before removal.
- The registry pins exact versions; upgrades are explicit (`POST /plugins/{id}/upgrade`) and produce a diff of manifest permission changes for re-approval.
- Multiple versions MUST NOT be simultaneously active for one plugin id.

**TITAN-INT-017 (MUST)** — A plugin upgrade that widens permissions MUST re-enter the approval step before activation. *Acceptance:* upgrade test with an added `network` pattern blocks activation until re-approved.

---

## 4. Tool Abstraction Layer

### 4.1 Tool registry

The Tool Registry is an in-memory index (rebuilt from PostgreSQL + plugin runtimes on boot) mapping tool names to: JSON-Schema definition, provider (built-in module / plugin id / MCP server id), risk class, required RBAC scopes, timeout, and rate limits. Names are namespaced: `fs.read`, `git.commit`, `github.open_pr`, `mcp.<server>.<tool>`.

Agents receive their tool list per turn, filtered to (agent role's allowed tools) ∩ (enabled providers) ∩ (project-level policy). The filtered definitions are handed to the LLM in the gateway's canonical tool-definition format (§8.1), which is JSON Schema — identical to what Anthropic/OpenAI tool-use APIs consume.

**TITAN-INT-020 (MUST)** — Every tool MUST be defined by a JSON Schema (draft 2020-12) for inputs and a declared output schema; the Tool Bus MUST validate arguments before dispatch and results before return. *Acceptance:* fuzz test sends malformed args to every registered tool; 100% are rejected pre-execution with schema-pointer error messages that are fed back to the calling agent for self-correction.

**Example tool definition (registry entry, abbreviated):**

```json
{
  "name": "git.commit",
  "provider": "builtin:git",
  "description": "Create a commit in the project workspace repository.",
  "risk": "write",
  "required_scopes": ["workspace:write"],
  "timeout_s": 30,
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "message":  { "type": "string", "minLength": 10 },
      "paths":    { "type": "array", "items": { "type": "string" } },
      "author_agent": { "type": "string" }
    },
    "required": ["message", "paths"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": { "commit_sha": { "type": "string" }, "files_changed": { "type": "integer" } },
    "required": ["commit_sha"]
  }
}
```

### 4.2 Tool invocation flow and wire format

```
Agent LLM turn
   |  (tool_use block)
   v
Tool Bus:  1. parse & schema-validate args
           2. authz: agent scopes ∩ tool scopes; project policy; risk gate
           3. write audit row (status=STARTED)
           4. dispatch -> Tool Host worker / plugin runtime / MCP client
           5. enforce timeout, capture stdout/stderr/artifacts
           6. schema-validate result; redact secrets
           7. finalize audit row; emit OTel span; return tool_result
   |
   v
Agent LLM next turn (tool_result block)
```

**Example tool-call envelope (as persisted and as sent to the executor):**

```json
{
  "tool_call_id": "tc_01J9ZK3W8QF4",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "project_id": "prj_netflix_clone",
  "task_id": "tsk_00412",
  "agent_id": "agent.backend_engineer.2",
  "tool": "git.commit",
  "arguments": {
    "message": "feat(api): add JWT refresh endpoint with rotation",
    "paths": ["services/api/src/auth/refresh.py", "services/api/tests/test_refresh.py"],
    "author_agent": "agent.backend_engineer.2"
  },
  "requested_at": "2026-07-04T10:42:17.031Z"
}
```

**Example result envelope:**

```json
{
  "tool_call_id": "tc_01J9ZK3W8QF4",
  "status": "ok",
  "duration_ms": 412,
  "result": { "commit_sha": "9f2c1ab", "files_changed": 2 },
  "stderr_tail": "",
  "artifacts": []
}
```

Errors use `status: "error"` with a machine-readable `code` (`SCHEMA_INVALID`, `PERMISSION_DENIED`, `TIMEOUT`, `SANDBOX_VIOLATION`, `UPSTREAM_ERROR`, `TOOL_UNAVAILABLE`) plus a human/LLM-readable `message` so agents can recover autonomously (Volume 6 retry policy).

### 4.3 Execution sandboxing

Tool Host workers execute built-in tools inside per-project workspace containers:

| Control | Policy |
|---|---|
| Filesystem | Workspace mounted at `/workspace`; everything else read-only or absent |
| Network | Deny-all except per-tool allowlist (e.g. `web.search` egress via research proxy) |
| Process | Non-root, no privileged flags, PID limit, `no_new_privileges` |
| Resources | Default 1 vCPU / 1 GiB / 60 s per call; per-tool overrides in registry |
| Docker access | Only via `docker.*` tools which talk to a rootless DinD/sysbox sidecar, never the host daemon socket |

**TITAN-INT-021 (MUST)** — Terminal and code-execution tools MUST run inside the workspace sandbox with no access to host filesystem, host Docker socket, or platform secrets. *Acceptance:* red-team suite from Volume 8 (`cat /etc/shadow`, socket probing, `docker.sock` mount attempt, env-var secret scraping) fully blocked; violations produce `SANDBOX_VIOLATION` audit events.

### 4.4 Risk classes and approval gates

Every tool declares `risk: read | write | admin`. Project policy (configurable; defaults below) maps risk to gating:

| Risk | Examples | Default gate |
|---|---|---|
| `read` | `fs.read`, `github.get_pr`, `web.search` | Auto-approved |
| `write` | `git.push`, `jira.create_issue`, `docker.build` | Auto-approved within workspace/project scope; rate-limited |
| `admin` | `github.merge_pr` to protected branch, `cloud.provision`, `k8s.apply` | Requires human approval or CEO-agent sign-off per Volume 6 release policy |

**TITAN-INT-022 (MUST)** — `admin`-risk tool calls MUST block on an approval record (human or policy-delegated) before dispatch. *Acceptance:* attempting `cloud.provision` without approval yields `PERMISSION_DENIED` and a pending-approval item in the dashboard.

### 4.5 Audit logging

**TITAN-INT-023 (MUST)** — Every tool call MUST produce an immutable audit record in PostgreSQL (`tool_audit` table: tool_call_id, trace_id, project, task, agent, tool, argument hash + redacted args JSONB, status, duration, result hash, initiator chain) and an OpenTelemetry span; secrets MUST be redacted before persistence. *Acceptance:* audit row exists for 100% of calls in a full autonomous project run; sampled rows show no secret material; Grafana/Loki dashboards can reconstruct any task's tool timeline (Volume 9).

**TITAN-INT-024 (SHOULD)** — Audit records SHOULD be hash-chained per project (each row stores `prev_hash`) to make tampering evident. *Acceptance:* verification job detects a mutated historical row.

---

## 5. Core Tool Set

These tools ship with core (as built-in modules under `titan/tools/*`), are always available offline, and require no third-party accounts — satisfying the local-first hard requirement.

| Namespace | Tools (representative) | Risk | Notes |
|---|---|---|---|
| `fs` | `read`, `write`, `list`, `glob`, `grep`, `patch` | read/write | Workspace-jailed; `patch` applies unified diffs atomically |
| `git` | `status`, `diff`, `log`, `branch`, `commit`, `merge`, `rebase`, `push`, `clone` | write | Local repo in workspace; remotes only via connector credentials |
| `terminal` | `run` (argv + env + cwd + timeout), `run_background`, `kill` | write | §4.3 sandbox; output streamed over WebSocket to the dashboard Logs view |
| `docker` | `build`, `run`, `compose_up`, `compose_down`, `logs`, `ps` | write | Rootless sidecar daemon per workspace |
| `browser` | `goto`, `snapshot` (accessibility-tree JSON), `screenshot`, `click`, `type`, `eval_js` | write | Headless Chromium via Playwright in its own container; used by QA agent for E2E and Research agent for docs |
| `web` | `search`, `fetch` (readability-extracted markdown) | read | Pluggable search backend: SearXNG (default, self-hosted, free) or provider API; egress via caching research proxy |
| `code` | `run_python`, `run_node`, `lint`, `test` | write | Convenience wrappers over `terminal.run` with structured result parsing (JUnit/coverage) |

**TITAN-INT-030 (MUST)** — Core tool set (`fs`, `git`, `terminal`, `docker`, `browser`, `web`, `code`) MUST be implemented at MVP with full schema definitions and sandbox conformance. *Acceptance:* the Volume 6 end-to-end scenario ("Build me a Netflix clone") completes using only core tools plus the GitHub connector, on a single laptop under Docker Compose.

**TITAN-INT-031 (MUST)** — `terminal.run` MUST take argv arrays (no shell string interpolation by default), enforce timeouts, cap output (1 MiB per stream, truncation marked), and stream incremental output as `tool.output` events on Redis pub/sub for live dashboard display. *Acceptance:* injection test with hostile filenames executes literally; a `yes`-spam command is truncated and killed at timeout.

**TITAN-INT-032 (SHOULD)** — `browser.snapshot` SHOULD return a token-efficient accessibility-tree representation (role/name/ref) rather than raw HTML, with `screenshot` available for vision-capable models. *Acceptance:* snapshot of a reference page is <10% of raw HTML token count and sufficient for the QA agent to complete a scripted checkout flow.

---

## 6. External Integration Specifications

All connectors below are first-party plugins (§3.1). Each stores its config via `config_schema`, its credentials in the secret store, and exposes tools + webhooks per its manifest.

### 6.1 GitHub (reference connector)

Capabilities: repos (create/clone/fork), branches, PRs (open/update/review/comment/merge), issues (CRUD, labels, milestones), checks/status reads, releases, webhooks.

- **Auth:** GitHub App (preferred; fine-grained installation tokens, higher rate limits) or PAT fallback for local/free-tier use. App private key lives in the secret store.
- **Tools contributed:** `github.create_repo`, `github.open_pr`, `github.update_pr`, `github.review_pr`, `github.merge_pr` (risk `admin` on protected branches), `github.create_issue`, `github.comment`, `github.get_checks`, `github.search_code`.
- **Webhooks:** `/hooks/github` verifying `X-Hub-Signature-256`; events `pull_request`, `issue_comment`, `check_suite`, `push` are translated into TITAN events (`vcs.pr.review_requested`, `vcs.checks.completed`, …) on Redis Streams, which drive the Volume 6 review/deploy workflow.
- **Rate limits:** connector maintains a token bucket from `X-RateLimit-*` headers; at <10% remaining, write tools return `UPSTREAM_ERROR/RATE_LIMITED` with `retry_after`, letting the orchestrator reschedule.

**TITAN-INT-040 (MUST)** — GitHub connector MUST support the full PR lifecycle (branch → PR → review comments → checks status → merge) and inbound webhooks with signature verification. *Acceptance:* integration test against a scratch org completes the lifecycle end-to-end; webhook with a bad signature is rejected 401 and audited.

### 6.2 GitLab

Mirror of §6.1 mapped to GitLab semantics (merge requests, pipelines, GitLab webhooks with `X-Gitlab-Token`). Tool names are provider-specific (`gitlab.open_mr`), but the connector also registers the **generic `vcs.*` aliases** (`vcs.open_change_request`, `vcs.merge`) so workflow definitions in Volume 6 remain provider-neutral. Self-hosted GitLab CE is the recommended free/local option. **TITAN-INT-041 (SHOULD)** — GitLab connector at parity with the `vcs.*` alias surface. *Acceptance:* Volume 6 workflow test passes with GitLab as the only VCS connector enabled.

### 6.3 Slack & Discord notifications

Purpose: outbound notifications (milestone completed, approval requested, build failed, release shipped) and lightweight inbound commands.

- **Slack:** Bot token (`chat:write`, `commands`); tools `slack.post_message`, `slack.post_approval` (Block Kit message with Approve/Reject buttons that resolve a pending `admin`-gate approval via the interactivity webhook). Slash command `/titan status <project>`.
- **Discord:** Bot + webhook URLs; tools `discord.post_message`, `discord.post_embed`; slash commands via Discord interactions endpoint.
- Both subscribe to the notification topics of the event bus; message templates are per-event and configurable.

**TITAN-INT-042 (MUST)** — Notification connectors MUST deliver event-driven messages with per-project channel routing and MUST support approval resolution from Slack. *Acceptance:* an `admin` tool gate is approved from a Slack button and the blocked tool call proceeds; delivery failures retry with exponential backoff (max 5) and then surface in the dashboard.

### 6.4 Jira & Linear sync

Purpose: mirror TITAN's internal task graph (Volume 6) into the customer's tracker so humans can watch progress in their own tools.

- **Direction:** TITAN is the source of truth for agent-created tasks; sync is two-way for status and comments (human comment in Jira → `task.comment_added` event → visible to PM agent).
- **Mapping table (stored per project):** TITAN task ⇄ Jira issue key / Linear issue id; TITAN states (`todo/in_progress/review/done/blocked`) mapped to tracker workflow states via configurable mapping in `config_schema`.
- **Jira:** REST v3 + OAuth 2.0 (3LO) or API token; webhooks for `jira:issue_updated`. **Linear:** GraphQL API + webhook signatures.
- **Conflict rule:** last-writer-wins with audit trail; simultaneous transition conflicts flag the task `sync_conflict` for PM-agent resolution rather than silently overwriting.

**TITAN-INT-043 (SHOULD)** — Jira and Linear connectors MUST (within this SHOULD-priority feature) keep status parity within 60 s under normal operation. *Acceptance:* soak test of 200 task transitions shows ≥99% mirrored within 60 s, zero lost updates, conflicts flagged not dropped.

### 6.5 Figma (read-only)

Purpose: give the Frontend Engineer agent design ground truth. Tools: `figma.get_file` (node tree), `figma.get_node_image` (rendered PNG for vision models), `figma.get_variables` (design tokens → Tailwind theme mapping), `figma.get_comments`. Auth: personal access token or OAuth. The connector is strictly read-only at MVP (`risk: read`), aligning with the "Figma read" scope. **TITAN-INT-044 (SHOULD)** — Figma read connector with token/variable extraction usable by the frontend agent to generate Tailwind config. *Acceptance:* given a reference Figma file, the extracted tokens produce a `tailwind.config.ts` whose palette matches the file's variables.

### 6.6 VS Code extension (concept)

A thin client, not a fork: the extension connects to a TITAN instance over its REST + WebSocket APIs (OAuth2 device flow).

Features (concept scope, post-MVP): project/task tree view; live agent activity feed; inline diff review of agent-proposed changes with Approve/Request-changes (resolves the same approval records as Slack); "hand off to TITAN" command that packages the current repo + a task description into a new TITAN task; workspace file sync via the `fs` tool API rather than direct disk access, preserving the audit chokepoint. **TITAN-INT-045 (MAY)** — VS Code extension implementing task view + diff approval. *Acceptance:* extension approves a pending change and the merge proceeds, with the approval attributed to the human's identity in the audit log.

### 6.7 Cloud providers

Deployment tools are connector plugins so core stays cloud-agnostic; Volume 9 owns the deployment workflows that call them.

| Connector | Tools (representative) | Notes |
|---|---|---|
| `titan.aws` | `cloud.provision` (Terraform/OpenTofu plan+apply), `s3.put`, `ecr.push`, `eks.kubeconfig` | All `admin` risk; plan output attached to approval request |
| `titan.gcp` | analogous (GCS, Artifact Registry, GKE) | |
| `titan.k8s` | `k8s.apply`, `k8s.rollout_status`, `k8s.logs` | Works against any cluster incl. local kind/k3d |
| `titan.local` | `deploy.compose` | Default target — free/local deployment via Docker Compose |

**TITAN-INT-046 (MUST)** — The default deploy target MUST be local Docker Compose requiring no cloud account; cloud connectors are optional plugins whose `admin` tools always attach an execution plan (e.g. `terraform plan`) to the approval gate. *Acceptance:* full platform demo deploys its generated app locally with zero cloud credentials configured.

---

## 7. MCP — Model Context Protocol Support

MCP is a **first-class plugin transport**: any MCP server becomes a TITAN plugin without writing TITAN-specific code.

- **Manifest:** `runtime.type: mcp` with `transport: stdio | http` and either `command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", ...]` or `url: https://...` (Streamable HTTP). All other manifest sections (permissions, scopes, risk overrides) apply identically.
- **Discovery:** on enable, the MCP client performs `initialize`, then `tools/list`; each MCP tool is registered as `mcp.<plugin_id>.<tool_name>` with its advertised JSON Schema. `notifications/tools/list_changed` triggers live re-registration. MCP resources are exposed to the Research/Memory agents as read tools (`mcp.<id>.read_resource`); MCP prompts are surfaced in the prompt library (Volume 2).
- **Sandboxing:** stdio servers run in the same per-plugin container regime as Python plugins (§3.5); HTTP servers are subject to the manifest egress allowlist. MCP tools default to `risk: write` unless the operator downgrades them per tool in configuration — external schemas are not trusted to self-declare risk.
- **Auth:** HTTP transport supports OAuth 2.1 per MCP spec; tokens live in the secret store.

**TITAN-INT-050 (MUST)** — TITAN MUST connect to spec-compliant MCP servers over stdio and Streamable HTTP, auto-registering their tools into the Tool Registry with full Tool Bus validation, gating, and audit applied. *Acceptance:* the reference `server-filesystem` and one HTTP MCP server are installed via manifest only; their tools appear in the registry, execute under audit, and a `PERMISSION_DENIED` fires when an agent lacking scopes calls one.

**TITAN-INT-051 (SHOULD)** — TITAN SHOULD also *expose* its own core tools as an MCP server (`titan mcp serve`) so external clients (Claude Code, IDEs) can drive a TITAN workspace. *Acceptance:* an external MCP client lists and invokes `fs.read` against a TITAN workspace with TITAN-side audit rows created.

---

## 8. Multi-LLM Provider Gateway

### 8.1 Unified interface

The gateway (`titan/gateway`) exposes one async Python interface and one internal HTTP endpoint; agents never see provider SDKs.

```
ChatRequest  { model_alias, messages[], tools[]?, tool_choice?, temperature?,
               max_tokens, stream?, metadata{agent_id, task_id, project_id} }
ChatResponse { text?, tool_calls[]?, finish_reason, usage{input_tokens,
               output_tokens, cached_tokens}, provider, model_id, cost_usd, latency_ms }
```

Canonical formats: messages follow a provider-neutral schema (system/user/assistant/tool roles; content parts for text, image, tool_use, tool_result); tool definitions are the §4.1 JSON Schemas. Adapters translate to/from each provider: **Anthropic** (Messages API), **OpenAI** (Chat Completions + Responses), **Google** (Gemini API), **Ollama** (local, OpenAI-compatible endpoint). Streaming is normalized to one internal delta-event shape and re-broadcast to the dashboard over WebSockets.

**TITAN-INT-060 (MUST)** — Adding or switching a provider/model for any agent MUST require configuration changes only — no code changes. *Acceptance:* CI matrix runs the same agent conversation (including tool use and streaming) against Anthropic, OpenAI, Google, and Ollama backends by swapping `models.yaml` alone.

**TITAN-INT-061 (MUST)** — The gateway MUST normalize tool-calling across providers, including parallel tool calls and provider-specific quirks (e.g. strict-mode schemas), behind the canonical format. *Acceptance:* golden-file tests per adapter verify round-tripping of a multi-tool, multi-turn transcript.

### 8.2 Configuration and per-agent routing

`config/models.yaml` (hot-reloadable; secrets by env/secret-store reference):

```yaml
providers:
  anthropic: { api_key: ${ANTHROPIC_API_KEY} }
  openai:    { api_key: ${OPENAI_API_KEY} }
  google:    { api_key: ${GOOGLE_API_KEY} }
  ollama:    { base_url: http://ollama:11434 }

model_aliases:
  reasoning-large:  { provider: anthropic, model: claude-opus-4-6 }
  coding-default:   { provider: anthropic, model: claude-sonnet-4-5 }
  fast-cheap:       { provider: google,    model: gemini-2.5-flash }
  local-default:    { provider: ollama,    model: qwen3-coder:30b }

routing:
  default: coding-default
  by_agent:
    agent.ceo:               reasoning-large
    agent.architect:         reasoning-large
    agent.backend_engineer:  coding-default
    agent.frontend_engineer: coding-default
    agent.qa:                fast-cheap
    agent.research:          fast-cheap
  fallbacks:
    coding-default:  [local-default]        # on outage / rate limit / budget cap
    reasoning-large: [coding-default, local-default]

budgets:
  per_project_usd: 25.00
  per_task_usd: 2.00
  action_on_exceeded: fallback_then_pause   # fallback to local, else pause task
```

**TITAN-INT-062 (MUST)** — Per-agent model routing with alias indirection MUST be supported, and a fully local profile (`routing.default: local-default` with Ollama) MUST run the entire platform with zero paid API keys. *Acceptance:* "local-only" CI profile completes a small end-to-end project using Ollama only.

### 8.3 Fallbacks and resilience

Fallback triggers: provider 5xx/timeouts (after per-request retries with jittered backoff), 429 rate limits, context-window overflow (falls back only to a larger-context alias), and budget-cap breach. Circuit breaker per provider (open after 5 failures/30 s; half-open probes). Every fallback is recorded on the response metadata and audit trail so evaluation (Volume 10) can attribute quality differences to model substitutions.

**TITAN-INT-063 (MUST)** — Automatic failover to configured fallback aliases with circuit breaking; agent work continues without human intervention when at least one healthy provider remains. *Acceptance:* chaos test blackholes the primary provider mid-task; task completes on fallback; audit shows the substitution.

### 8.4 Cost and token accounting

Every `ChatResponse.usage` is written to `llm_usage` (PostgreSQL): timestamp, project, task, agent, provider, model, input/output/cached tokens, computed `cost_usd` from a versioned price table (`config/prices.yaml`, updatable without deploy; Ollama priced at 0 with GPU-seconds tracked instead). Redis maintains rolling counters for budget enforcement at request time. Prometheus exports `titan_llm_tokens_total`, `titan_llm_cost_usd_total`, `titan_llm_latency_seconds` labeled by provider/model/agent; Grafana ships a per-project cost dashboard (Volume 9).

**TITAN-INT-064 (MUST)** — Token and cost accounting per call, aggregated per agent/task/project, with hard budget caps enforced pre-request. *Acceptance:* a task hitting `per_task_usd` transitions to fallback-or-pause per policy before exceeding the cap by more than one maximal request; dashboard totals reconcile with provider-reported usage within 2%.

---

## 9. Decision Records

### ADR-INT-01 — Out-of-process plugins over in-process imports

- **Context:** Plugins run third-party code adjacent to secrets and workspaces; TITAN must stay stable and local-first.
- **Decision:** All plugin code executes out-of-process (per-plugin venv + subprocess/container) speaking JSON-RPC over stdio, mirroring MCP framing.
- **Alternatives considered:** (a) In-process Python entrypoints — fastest calls, but a crash or dependency conflict kills the orchestrator and permissions become unenforceable; (b) WASM sandbox — strong isolation but immature Python-guest ecosystem and blocks native deps that connectors need.
- **Trade-offs:** ~1–5 ms IPC overhead per call and process-management complexity, accepted in exchange for crash isolation, enforceable egress/permission boundaries, and one shared codepath with MCP.

### ADR-INT-02 — MCP as first-class transport rather than a bridge plugin

- **Context:** The ecosystem of MCP servers is large and growing; TITAN needs maximal integrations for minimal maintenance.
- **Decision:** The plugin runtime natively speaks MCP (`runtime.type: mcp`); MCP tools are ordinary registry entries with TITAN gating layered on top.
- **Alternatives considered:** (a) A single "MCP bridge" plugin — hides MCP servers from the permission model, making per-server egress/secret grants impossible; (b) Ignore MCP and hand-write connectors — unbounded maintenance burden.
- **Trade-offs:** TITAN must track MCP spec revisions and cannot trust external schemas' risk semantics (mitigated by write-by-default risk, §7); in exchange, hundreds of existing servers work day one.

### ADR-INT-03 — Build a thin in-house gateway instead of adopting LiteLLM/OpenRouter

- **Context:** Canonical decision requires provider-agnostic access to Anthropic, OpenAI, Google, and Ollama with config-only switching, per-agent routing, budgets, and audit metadata tied to TITAN's task graph.
- **Decision:** Implement a thin gateway in `titan/gateway` with four adapters and TITAN-native routing/budget/audit hooks.
- **Alternatives considered:** (a) LiteLLM proxy — broad coverage, but adds a service to the local stack, and per-agent routing/budgets keyed to TITAN entities would still need a wrapper; (b) OpenRouter — hosted-only, conflicts with local-first and free-tier requirements.
- **Trade-offs:** We own adapter maintenance for four providers (bounded, versioned APIs) and forgo LiteLLM's long tail; in exchange we get zero extra runtime dependencies, first-class task/agent attribution, and no data leaving the deployment by default.

### ADR-INT-04 — Capability-based deny-by-default permissions with dual-layer checks

- **Context:** Autonomous agents plus third-party plugins is a compound risk: either can be confused or malicious.
- **Decision:** Permissions are granted to plugins at enable time (operator-approved manifest) AND checked against the calling agent's RBAC scopes at call time; risk classes add human gates for `admin` actions.
- **Alternatives considered:** (a) Trust first-party plugins implicitly — creates two security regimes and untested third-party paths; (b) Prompt-level guardrails only — LLM instructions are not a security boundary.
- **Trade-offs:** More approval friction and manifest verbosity; accepted because a single laundering path (low-privilege agent → high-privilege plugin) would defeat the entire Volume 8 threat model.

---

## 10. Risks & Open Questions

| # | Risk / Question | Impact | Mitigation / Owner |
|---|---|---|---|
| R1 | **Malicious or compromised plugin** exfiltrates secrets within its granted egress allowlist. | High | Narrow allowlists, per-plugin secrets, egress proxy logging with anomaly alerts (Vol 8); signing (TITAN-INT-012). |
| R2 | **Prompt injection via tool results** (web pages, Jira comments, MCP resources) steering agents into harmful tool calls. | High | Result sanitization markers, risk gates on `write`/`admin`, injection red-team suite shared with Volume 8. |
| R3 | **MCP spec drift** (auth, transport revisions) breaking auto-registered servers. | Medium | Pin supported spec versions per release; conformance tests against reference servers in CI. |
| R4 | **Provider API divergence** (new tool-call semantics, deprecations) silently degrading agents. | Medium | Golden-file adapter tests (TITAN-INT-061) run nightly against live sandboxes; price table versioning. |
| R5 | **Local model quality** insufficient for `local-default` fallback, causing silent quality collapse when budgets trip. | Medium | Fallback substitutions are audited and surfaced in dashboards; Volume 10 evaluation harness scores fallback runs. |
| R6 | **Rate-limit starvation** on shared GitHub App installations across many parallel agents. | Medium | Central token buckets per connector; orchestrator backpressure via `retry_after`. |
| Q1 | Should plugin UI panels support a sandboxed module-federation path (richer than iframes) in dashboard v2? | — | Decide with Volume 4 owners after MVP telemetry on panel usage. |
| Q2 | Do we allow plugins to contribute *workflow steps* (Volume 6) in `titan_api` 1.x, or defer to 2.0? | — | Deferred to 2.0 unless two first-party connectors need it; tracked in the decision log. |
| Q3 | Marketplace distribution/signing authority model (central index vs. federated). | — | Volume 10 scope; §3.2 signing format designed to be forward-compatible either way. |

---

## 11. Cross-References

- **Volume 1 — Foundation:** owns and justifies all canonical stack decisions referenced here (FastAPI, PostgreSQL/Redis/Qdrant, Redis Streams, OAuth2/RBAC, local-first mandate).
- **Volume 2 — Multi-Agent Intelligence:** agent roles, per-agent tool allowlists, and prompt templates that consume the Tool Registry definitions of §4.
- **Volume 4 — Frontend & Dashboard:** Plugin Manager screens, extension slots for `ui_panels`, live tool-output Logs view, cost dashboards.
- **Volume 5 — Memory, Knowledge & RAG:** Research agent's use of `web.*` and MCP resources; storage of tool-derived knowledge in Qdrant.
- **Volume 6 — Autonomous Development Workflow:** task graph events that connectors mirror (Jira/Linear), VCS webhook-driven review/merge flow, retry policy on tool errors, approval gates.
- **Volume 8 — Security:** threat model behind the sandbox and permission requirements; secret store; injection red-teaming; audit retention policy.
- **Volume 9 — Deployment & Production:** OTel/Prometheus/Grafana wiring for tool spans and LLM cost metrics; cloud connector usage in CI/CD; scaling the Tool Host on Kubernetes.
- **Volume 10 — Autonomous Evolution:** plugin marketplace, model performance benchmarking that consumes §8.4 accounting data, and agent self-evaluation of fallback runs.

*End of Volume 7 — TITAN-VOL-07, Draft v1.0.*
