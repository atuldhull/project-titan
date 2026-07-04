# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 8 — Security Specification

| Field | Value |
|---|---|
| Series | PROJECT TITAN — Autonomous Software Engineering Platform (10 volumes) |
| Volume | 8 — Security |
| Doc ID | TITAN-VOL-08 |
| Requirement prefix | TITAN-SEC-NNN |
| Status | Draft v1.0 |
| Date | 2026-07-04 |
| Audience | Platform engineers, security engineers, AI coding agents implementing TITAN |

**Abstract.** TITAN autonomously writes, reviews, executes, and deploys software, which makes it both a high-value target and an unusual attack surface: it runs LLM-generated code, grants agents tool access, and holds credentials for external systems. This volume specifies TITAN's security architecture end to end: a STRIDE threat model extended with AI-specific threats; OAuth2/OIDC + JWT authentication; RBAC with a full permission matrix; sandboxing for agent code execution; local-first secret management; encryption; validation and output encoding; rate limiting; immutable audit logging; CI supply-chain scanning; OWASP Top 10 and OWASP LLM Top 10 mappings; an incident response runbook; security testing; and a secure-defaults checklist. All requirements carry TITAN-SEC-NNN identifiers with MUST/SHOULD priorities and acceptance criteria. Stack choices are canonical per Volume 1; this volume specifies how they are secured.

---

## Table of Contents

1. Introduction, Scope, and Security Principles
2. Threat Model (STRIDE + AI-Specific)
3. Authentication and Authorization Architecture
4. RBAC Roles and Permission Matrix
5. Agent Execution Sandboxing
6. Secret Management
7. Encryption at Rest and in Transit
8. Input Validation and Output Encoding
9. Rate Limiting and Abuse Prevention
10. Audit Logging
11. Supply Chain Security and CI Scanning
12. OWASP Compliance Mapping
13. Incident Response Runbook
14. Security Testing
15. Secure Defaults Checklist
16. Risks and Open Questions
17. Cross-References

---

## 1. Introduction, Scope, and Security Principles

### 1.1 Scope

This volume governs the security of the TITAN platform itself: the FastAPI orchestrator, agent runtime, LLM gateway, Next.js dashboard, data stores (PostgreSQL 16, Redis 7, Qdrant), CI pipelines, and both deployment targets (Docker Compose locally, Kubernetes in production). Security gates for software TITAN *generates* are wired into the workflow in Volume 6 and governed here in §11.

### 1.2 Security Principles

1. **Assume the model is compromised.** Any LLM output — text, code, tool calls — may be attacker-influenced. LLM output is untrusted input, always.
2. **Least privilege.** Users, agents, services, and containers get the minimum permissions their role requires, scoped per project and per task.
3. **Defense in depth.** Prompt hardening is never the only barrier; every AI-specific threat has at least one non-LLM enforcement layer.
4. **Local-first without security debt.** The single-laptop Docker Compose deployment must be secure by default with free tooling.
5. **Secure by default.** Dangerous capabilities (sandbox egress, auto-merge, auto-deploy) ship disabled and require explicit opt-in.
6. **Auditability.** Every security-relevant action is attributable and recorded immutably.

- **TITAN-SEC-001 (MUST):** Every LLM response (text or tool call) MUST pass validation (§8) and authorization (§4) before producing any side effect. *Acceptance:* no code path executes an LLM-proposed action without the tool-authorization layer; a hostile mock LLM's unauthorized tool calls are rejected in integration tests.
- **TITAN-SEC-002 (MUST):** All controls in this volume MUST function in local Docker Compose using only free/open-source components. *Acceptance:* clean-laptop `docker compose up` passes the §15 checklist with zero paid services.

---

## 2. Threat Model (STRIDE + AI-Specific)

### 2.1 Assets and Trust Boundaries

Assets: (A1) user and generated source code; (A2) credentials — LLM API keys, GitHub tokens, deploy keys; (A3) the orchestrator's authority to execute code and call tools; (A4) memory/RAG stores feeding future agent context; (A5) the audit trail; (A6) availability and LLM budget.

```
                        TRUST BOUNDARY MAP
 ┌──────────────────────────────────────────────────────────────────┐
 │ Internet                                                         │
 │   [User Browser]      [External LLM APIs]   [GitHub/Registries]  │
 └──────┬────────────────────▲──────────────────────▲───────────────┘
   TB1  │ HTTPS/WSS      TB4 │ egress only      TB5 │ egress only
 ┌──────▼────────────────────┴──────────────────────┴───────────────┐
 │ TITAN Core (trusted)                                             │
 │  [Next.js 15 UI]──[FastAPI Orchestrator]──[LLM Gateway]          │
 │        │                  │        │                             │
 │        │            TB2   │        └──[Postgres][Redis][Qdrant]  │
 │        │       ┌──────────▼───────────┐                          │
 │        │       │ Agent Sandbox Pool   │  ◄── TB2: LLM-generated  │
 │        │       │ (UNTRUSTED code,     │       code runs here,    │
 │        │       │  per-task containers)│       never in core      │
 │        │       └──────────────────────┘                          │
 │  TB3: agent writes into memory/RAG are quarantined & labeled     │
 └──────────────────────────────────────────────────────────────────┘
```

Boundaries: **TB1** browser↔API; **TB2** orchestrator↔sandbox (most critical); **TB3** agent outputs↔persistent memory (poisoning vector); **TB4** gateway↔LLM providers (context leaves our control); **TB5** platform↔registries and Git hosts.

### 2.2 STRIDE Analysis

| STRIDE class | Representative TITAN threats | Mitigations |
|---|---|---|
| **S**poofing | Stolen JWT replay; agent impersonation on Redis Streams; forged webhooks | Short-lived rotating tokens (§3); signed message envelopes (§3.5); webhook HMAC (§8) |
| **T**ampering | Queue payload modification; altering code between review and merge; poisoning Qdrant memory | Envelope signing (§3.5); artifact content hashes (§11); memory provenance tiers (§2.3) |
| **R**epudiation | Agent or user denies destructive action; no LLM-output-to-side-effect linkage | Immutable audit log with actor + task + trace ID (§10) |
| **I**nformation disclosure | Secrets in prompts/logs/providers; cross-project RAG bleed; verbose errors | Redaction pipeline (§6.4); per-project memory namespaces; generic errors (§8); encryption (§7) |
| **D**enial of service | Agent loops burning LLM budget; sandbox fork bombs; WSS floods | Budget caps + loop detection (§9); cgroup limits (§5); rate limiting (§9) |
| **E**levation of privilege | Sandbox escape; agent calling tools beyond role; SQLi via generated migrations; IDOR | Hardened runtime (§5); server-side tool allowlists (§4.3); parameterized SQL (§8); object-level authz (§4) |

### 2.3 AI-Specific Threats

Each maps to OWASP LLM Top 10 in §12.2.

**T-AI-1 — Prompt injection (direct/indirect).** A project brief, fetched README, issue comment, or dependency doc may carry hostile instructions; because agents chain outputs, one injected document can steer the organization. *Mitigations:* retrieved/external content is tagged `untrusted` in prompt assembly (soft control); **hard controls** — model-independent tool authorization (§4.3), egress-deny sandboxes (§5), redaction so context holds nothing worth exfiltrating (§6.4), human approval gates for irreversible actions (Volume 6).

**T-AI-2 — Malicious or vulnerable generated code.** Backdoors, unsafe patterns, logic bombs. *Mitigations:* generated code executes only in sandboxes (§5); mandatory SAST + Code Reviewer and Security Agent passes before merge (§11, §14); never runs in the orchestrator process.

**T-AI-3 — Tool-call abuse.** A confused or injected agent invokes `git push --force`, `delete_project`, or shell tools outside its mandate. *Mitigations:* per-role server-side allowlists with parameter validation — the gateway rejects out-of-list calls before execution; destructive tools require an approval token (§4.4); all calls audited.

**T-AI-4 — Secret leakage into LLM context.** Repo files or logs containing secrets get embedded in prompts and cross TB4. *Mitigations:* secrets never enter agent context by construction (§6.3); pre-send redaction on every gateway request (§6.4); Ollama routing for sensitive projects.

**T-AI-5 — Supply-chain risk in generated dependencies.** Typosquatted, hallucinated ("slopsquatted"), or vulnerable packages proposed by agents. *Mitigations:* dependency gate — registry existence, age/popularity thresholds, vulnerability scan, pin + hash lock before install (§11.5).

**T-AI-6 — Memory/RAG poisoning.** Malicious "lessons learned" written into organizational memory influence all future projects. *Mitigations:* provenance metadata on every memory record (author, task, project, trust tier); cross-project retrieval only from human-promoted `curated` tier; per-project namespaces by default (Volume 5).

- **TITAN-SEC-003 (MUST):** The threat model MUST be re-reviewed at every minor release and whenever a new tool, agent type, or trust boundary is added. *Acceptance:* release checklist item; doc history shows updates.
- **TITAN-SEC-004 (MUST):** Every T-AI threat MUST have at least one mitigation that does not depend on LLM behavior. *Acceptance:* traceability table T-AI-1..6 → non-LLM controls; red-team tests (§14.4) verify each.

---

## 3. Authentication and Authorization Architecture

### 3.1 Overview

Humans authenticate via OAuth2/OIDC; sessions use JWT bearer tokens (canonical, Volume 1). Locally TITAN ships an embedded OIDC-compatible auth service (FastAPI + authlib, username/password + TOTP); production federates to any OIDC IdP (Keycloak as the free reference, or Google/GitHub/Okta).

```
  Browser            Next.js 15 (BFF)          FastAPI API           IdP (OIDC)
    │  GET /login          │                        │                    │
    ├─────────────────────►│  redirect (PKCE) ──────┼───────────────────►│
    │◄─────────────── auth code ────────────────────┼────────────────────┤
    ├── code ─────────────►│── token exchange ──────┼───────────────────►│
    │                      │◄── id/access/refresh ──┼────────────────────┤
    │◄─ Set-Cookie ────────┤  (httpOnly, Secure,    │                    │
    │   session cookies    │   SameSite=Lax)        │                    │
    ├── API/WSS calls ────►│── JWT access token ───►│ verify sig+claims  │
```

### 3.2 Requirements — Authentication

- **TITAN-SEC-010 (MUST):** Login uses OAuth2 Authorization Code + PKCE; implicit and password grants disabled. *Acceptance:* implicit-flow attempt returns error.
- **TITAN-SEC-011 (MUST):** Access tokens are JWTs signed RS256 or EdDSA, lifetime ≤ 15 min, with `sub, org, roles, iat, exp, aud, iss, jti`; verifiers reject `alg=none` and HS256. *Acceptance:* unit tests reject none/HS256, expired, wrong `aud`/`iss`.
- **TITAN-SEC-012 (MUST):** Refresh tokens are opaque, single-use, rotated with reuse detection: presenting a consumed token revokes the family. Lifetime ≤ 14 days. *Acceptance:* replay test triggers family revocation + audit event.
- **TITAN-SEC-013 (MUST):** Browser tokens live only in `httpOnly, Secure, SameSite=Lax` cookies via the Next.js BFF — never in `localStorage` or client JS. *Acceptance:* frontend audit; cookie flags verified.
- **TITAN-SEC-014 (MUST):** Revocation takes effect ≤ 60 s via a Redis denylist of revoked `jti`/families checked per request. *Acceptance:* revoke-then-call fails within 60 s.
- **TITAN-SEC-015 (SHOULD):** TOTP MFA available in the embedded IdP; MUST be requireable per-org for Admin/Owner. *Acceptance:* policy toggle enforces MFA at next privileged login.
- **TITAN-SEC-016 (MUST):** WebSockets authenticate with the same JWT at handshake and close ≤ 60 s after expiry/revocation unless re-authenticated in-band. *Acceptance:* expired-token WSS refused; live connection closes on revocation.
- **TITAN-SEC-017 (MUST):** Failed logins throttled (per-account and per-IP backoff after 5 failures); embedded-IdP passwords hashed with Argon2id. *Acceptance:* brute-force test triggers backoff; DB shows Argon2id.

### 3.3 Service and Agent Identity

- **TITAN-SEC-018 (MUST):** Each agent instance runs with a service token scoped to `(agent_role, project_id, task_id)`, minted at task assignment, lifetime ≤ task timeout, never reused. *Acceptance:* a Backend-Engineer-scoped token calling a deploy endpoint is rejected.
- **TITAN-SEC-019 (MUST):** Inter-service auth (orchestrator↔gateway↔sandbox controller) uses mTLS in Kubernetes; local Compose MAY use isolated Docker networks + service tokens. *Acceptance:* plaintext inter-service call refused in prod profile.

### 3.4 Authorization Model

One enforcement point: a FastAPI dependency `require_permission(resource, action)` backed by the RBAC engine (§4), evaluated from JWT claims plus object ownership. The UI hides what you cannot do; the API enforces it.

### 3.5 Message Integrity on the Queue

- **TITAN-SEC-020 (MUST):** Every Redis Streams (and NATS JetStream at scale-out) message carries a signed envelope `{msg_id, sender_principal, task_id, ts, payload_sha256, hmac}` keyed per-service from the vault; consumers drop and audit invalid signatures. *Acceptance:* tampered message rejected + audit event.

### ADR-SEC-01 — Token strategy: short-lived JWT + rotating refresh via BFF cookies

- **Context:** TITAN's dashboard renders LLM-derived content, raising XSS exposure; tokens in `localStorage` would be fatal under XSS.
- **Decision:** Next.js acts as Backend-for-Frontend holding tokens in httpOnly cookies; access JWTs ≤ 15 min; opaque single-use refresh tokens with family revocation; Redis denylist for immediate revocation.
- **Alternatives:** (a) long-lived stateless JWT — no revocation, rejected; (b) server-side sessions only — poor fit for WSS + service calls and horizontal scaling; (c) DPoP sender-constrained tokens — strongest, deferred pending library maturity.
- **Trade-offs:** per-request Redis lookup (sub-ms, already in stack) and one extra BFF hop; accepted for real revocation and XSS containment.

---

## 4. RBAC Roles and Permission Matrix

### 4.1 Roles

Human roles (org-scoped, project-assignable): **Owner, Admin, Maintainer, Developer, Viewer**. Machine role class: **Agent**, parameterized by agent type (§4.3). Roles are additive-only — no deny rules — keeping evaluation auditable.

### 4.2 Permission Matrix (humans)

Legend: ✔ allowed · A = allowed with approval workflow · — denied.

| Resource / Action | Owner | Admin | Maintainer | Developer | Viewer |
|---|---|---|---|---|---|
| Org: delete org, billing/plan | ✔ | — | — | — | — |
| Org: manage members & roles | ✔ | ✔ | — | — | — |
| Org: security policy (MFA, egress, retention) | ✔ | ✔ | — | — | — |
| Project: create / delete | ✔ | ✔ | ✔ / — | — | — |
| Project: configure agents & LLM providers | ✔ | ✔ | ✔ | — | — |
| Tasks: create, edit, reprioritize | ✔ | ✔ | ✔ | ✔ | — |
| Read code, dashboards, logs | ✔ | ✔ | ✔ | ✔ | ✔ |
| Approve merge of agent PRs | ✔ | ✔ | ✔ | A | — |
| Trigger production deploy | ✔ | ✔ | A | — | — |
| Secrets: create/rotate (write-only) | ✔ | ✔ | ✔ | — | — |
| Secrets: read plaintext | — | — | — | — | — |
| Audit log: read | ✔ | ✔ | — | — | — |
| Audit log: modify/delete | — | — | — | — | — |
| Plugins: install / configure | ✔ | ✔ | A | — | — |
| Sandbox policy: relax (e.g., egress) | ✔ | ✔ | A | — | — |
| Memory: promote to org-wide tier | ✔ | ✔ | ✔ | A | — |

Deliberate rows: **no role reads secret plaintext** (write-only; only sandbox injection reads values, §6.3) and **no role alters the audit log**.

### 4.3 Agent Tool Allowlists

The tool gateway rejects out-of-list calls regardless of what the model asks for.

| Agent type | Allowed tool classes | Explicitly denied |
|---|---|---|
| CEO / PM / Project Manager | task CRUD, messaging, docs read | shell, git write, deploy, secrets |
| Architect / Research | repo read, web fetch (allowlisted domains), docs write | shell, git push, deploy |
| Backend / Frontend / DB Engineer | sandbox shell, repo read/write on task branch, test run | protected-branch push, deploy, plugins |
| QA | sandbox shell, test run, repo read, bug report | git write outside test dirs, deploy |
| Security | scanners, repo read, report write | deploy, secret read |
| Code Reviewer | repo read, PR review API | any code write, deploy |
| DevOps | IaC repo write, deploy tool (approval token required) | secret plaintext read |
| Memory / Documentation | memory & docs read/write in namespace | shell, git, deploy |

### 4.4 Requirements

- **TITAN-SEC-030 (MUST):** RBAC enforced server-side on every route and tool invocation via a single middleware; UI checks are advisory. *Acceptance:* automated suite calls every route with every role against a matrix-as-code fixture.
- **TITAN-SEC-031 (MUST):** Agent tool calls validated against allowlist AND task project scope AND parameter constraints (e.g., git writes only to `titan/task-*` branches). *Acceptance:* out-of-scope fuzz calls 100% rejected + audited.
- **TITAN-SEC-032 (MUST):** Object-level authorization on every ID reference — no IDOR. *Acceptance:* org-A token against org-B resources returns 404.
- **TITAN-SEC-033 (MUST):** Irreversible actions (prod deploy, project delete, force-push, sandbox policy relaxation) require a single-use approval token (≤ 15 min TTL) minted by the human approval flow. *Acceptance:* action without token fails; replay fails.
- **TITAN-SEC-034 (SHOULD):** Quarterly access review via an auto-generated report (members, roles, last activity). *Acceptance:* report endpoint exists.

---

## 5. Agent Execution Sandboxing

All LLM-generated code — builds, tests, scripts, migrations — executes in ephemeral per-task sandboxes. Nothing generated ever runs in the orchestrator process or directly on the host.

```
 ┌─────────────────────────── Sandbox Node / Laptop ──────────────────────────┐
 │  Sandbox Controller (trusted)                                              │
 │   │ creates per task                                                       │
 │   ▼                                                                        │
 │  ┌─ sandbox: task-4711 ────────────────────────────────┐                   │
 │  │ runtime: gVisor(runsc) [prod] / hardened runc [dev] │                   │
 │  │ user: uid 10001 non-root · rootfs read-only         │                   │
 │  │ /workspace (rw, task branch checkout, size-capped)  │                   │
 │  │ caps: ALL dropped · no-new-privileges · seccomp     │                   │
 │  │ cgroups: cpu 2 · mem 2Gi · pids 256 · disk 5Gi      │                   │
 │  │ network: default DENY ─── proxy ──► allowlist:      │                   │
 │  │   registry mirror (pypi/npm), git remote (per task) │                   │
 │  │ lifetime ≤ task timeout; destroyed after task       │                   │
 │  └─────────────────────────────────────────────────────┘                   │
 │  K8s prod: dedicated node pool, PodSecurity "restricted",                  │
 │  NetworkPolicy default-deny, no serviceaccount token mounted               │
 └────────────────────────────────────────────────────────────────────────────┘
```

- **TITAN-SEC-040 (MUST):** Generated code executes only inside per-task sandboxes created/destroyed by the Sandbox Controller; never shared across tasks or projects. *Acceptance:* two sequential tasks show distinct container IDs and clean workspaces; orchestrator has no exec path for generated code.
- **TITAN-SEC-041 (MUST):** Sandboxes run non-root, all capabilities dropped, `no-new-privileges`, read-only rootfs (writable `/workspace`, `/tmp` only), seccomp at least runtime-default strict. *Acceptance:* in-sandbox probe fails privilege escalation and rootfs writes.
- **TITAN-SEC-042 (MUST):** Per-sandbox limits: CPU ≤ 2, memory ≤ 2 GiB, PIDs ≤ 256, disk ≤ 5 GiB, wall-clock ≤ task timeout (default 30 min); overridable only by Admin policy. *Acceptance:* fork-bomb and memory-hog tests killed without host impact.
- **TITAN-SEC-043 (MUST):** Sandbox network defaults to deny-all egress; only the package-registry mirror and the task's git remote are reachable, via an authenticating, logging forward proxy. Direct egress requires Admin opt-in per project (SEC-033 approval). *Acceptance:* in-sandbox `curl https://example.com` fails; registry install succeeds via proxy with log entries.
- **TITAN-SEC-044 (MUST):** Sandboxes never mount the Docker socket, Kubernetes service-account tokens, or any credentials beyond task-scoped injected secrets (§6.3). *Acceptance:* in-sandbox filesystem scan finds neither.
- **TITAN-SEC-045 (MUST):** In Kubernetes, sandboxes run in a dedicated namespace/node pool with PodSecurity `restricted`, default-deny NetworkPolicies, and gVisor RuntimeClass where supported. *Acceptance:* non-compliant pod rejected by admission.
- **TITAN-SEC-046 (SHOULD):** Sandbox stdout/stderr streams through the redaction filter (§6.4) before logs, UI, or LLM context. *Acceptance:* planted secret in sandbox output appears redacted at all sinks.

### ADR-SEC-02 — Sandbox runtime: containers, gVisor in production, hardened runc locally

- **Context:** TITAN must run untrusted generated code on a single laptop (free) and in production Kubernetes.
- **Decision:** Hardened Docker containers everywhere (SEC-041/042/043); production adds gVisor (`runsc`) for syscall interposition; local Compose uses hardened runc (gVisor optional).
- **Alternatives:** (a) Firecracker microVMs — strongest isolation but KVM-dependent and laptop-hostile; documented as scale-out option; (b) WASM/WASI — cannot run arbitrary toolchains (venvs, node_modules, compilers); (c) plain containers only — insufficient against kernel exploits for a platform whose core feature is running attacker-influenceable code.
- **Trade-offs:** gVisor costs ~10–30% on I/O-heavy builds with occasional compatibility gaps — accepted in prod. Local dev accepts container-grade isolation (the laptop user is the only victim), compensated by egress-deny networking enforced at the proxy.

---

## 6. Secret Management

### 6.1 Secret Classes

(1) Platform: DB/Redis/Qdrant credentials, JWT signing keys, HMAC keys. (2) Integration: LLM provider keys, GitHub tokens, registry credentials, deploy keys. (3) User project secrets: env vars for generated apps.

### 6.2 Vault Architecture (local-first)

- **TITAN-SEC-050 (MUST):** All secrets live in a dedicated store — never in code, git, plain config, Redis, or Postgres application tables. Local default: **SOPS + age** encrypted files unlocked at boot into an in-memory secrets service; production: HashiCorp Vault / OpenBao or cloud managers behind the same interface. *Acceptance:* gitleaks finds nothing in repo or image layers; only the secrets service holds plaintext, in memory.
- **TITAN-SEC-051 (MUST):** Write-only semantics for humans and APIs: values can be created, rotated, deleted — never read back via any API/UI. *Acceptance:* read attempts return 405/404.
- **TITAN-SEC-052 (MUST):** Every secret has metadata: owner, scope (org/project/task), timestamps, rotation period (default 90 days) with dashboard expiry warnings. *Acceptance:* overdue secrets raise alerts.

### 6.3 Delivery to Workloads

- **TITAN-SEC-053 (MUST):** Injection at execution time only, scoped to the consumer: platform services get env at boot; sandboxes get only the secrets their task's tool grants require, as tmpfs files destroyed with the sandbox. The LLM gateway alone holds provider keys — agents never see them. *Acceptance:* sandbox env/filesystem dump for a docs task contains zero integration secrets; only the gateway has provider-key env vars.

### 6.4 Keeping Secrets Out of Prompts and Logs

Enforcement layer for T-AI-4 and the "never in prompts/logs" rule.

- **TITAN-SEC-054 (MUST):** A redaction pipeline runs on (a) every context bundle before the gateway sends it to any provider, (b) every log line before Loki ingestion, (c) every sandbox output stream. It combines exact-match against the live secret inventory (hashed lookup), high-entropy detection, and provider-key patterns (`sk-ant-*`, `ghp_*`, `AKIA*`, …), replacing matches with `[REDACTED:<class>:<hash8>]`. *Acceptance:* planted secrets of each class appear redacted at every sink, including captured gateway payloads.
- **TITAN-SEC-055 (MUST):** Secret-bearing file patterns (`.env*`, `*.pem`, `*_key*`, `id_rsa*`, cloud credential paths) are excluded from context assembly and RAG ingestion by default. *Acceptance:* repo with `.env` shows the file absent from prompts and Qdrant.
- **TITAN-SEC-056 (SHOULD):** Orgs can pin sensitive projects to local models (Ollama) via gateway routing policy so context never leaves the machine. *Acceptance:* policy set → gateway log shows only local endpoints for that project.

### ADR-SEC-03 — Local-first vault: SOPS+age default, Vault/OpenBao behind one interface

- **Context:** "Local-first and free" is a hard requirement; Vault is heavy for a laptop, but production needs dynamic secrets and access audit.
- **Decision:** A thin `SecretsProvider` interface (get-by-reference, rotate, list-metadata). Default driver: SOPS+age encrypted YAML (zero infrastructure, git-storable ciphertext). Production driver: Vault/OpenBao. Switching is configuration-only, mirroring the LLM gateway philosophy.
- **Alternatives:** (a) Vault everywhere — too heavy locally; (b) plain `.env` locally — unencrypted at rest, normalizes bad practice; (c) OS keychains — inconsistent across platforms and headless Docker.
- **Trade-offs:** SOPS lacks dynamic secrets and fine-grained audit — acceptable with a single local operator; the interface seam makes the production upgrade invisible to call sites.

---

## 7. Encryption at Rest and in Transit

- **TITAN-SEC-060 (MUST):** All external traffic (browser, WSS, webhooks, LLM providers, git) uses TLS 1.2+ (prefer 1.3) with HSTS (`max-age` ≥ 15552000) on public endpoints; plain HTTP redirects or is disabled. *Acceptance:* `testssl.sh` grade A; no non-loopback cleartext listeners outside the dev profile.
- **TITAN-SEC-061 (MUST):** Production intra-cluster traffic is mTLS (SEC-019); Postgres, Redis, and Qdrant require TLS + auth in production profiles. *Acceptance:* non-TLS connection to prod Postgres refused.
- **TITAN-SEC-062 (MUST):** At rest: production data volumes and backups use storage-layer encryption (LUKS/cloud volume encryption); backups are age/GPG-encrypted before leaving the host. *Acceptance:* backup artifact is ciphertext.
- **TITAN-SEC-063 (MUST):** Field-level AES-256-GCM (key from vault) for highest-sensitivity columns: stored OAuth tokens, webhook signing secrets. Key rotation via key-version tagging. *Acceptance:* DB dump shows ciphertext; rotation test decrypts old rows with old key version.
- **TITAN-SEC-064 (SHOULD):** JWT signing keys rotate ≤ every 90 days with JWKS publishing. *Acceptance:* staging rotation without auth downtime.

---

## 8. Input Validation and Output Encoding

- **TITAN-SEC-070 (MUST):** Every API request and queue message is validated by Pydantic v2 models with `extra="forbid"`, explicit types, length bounds, and enum/pattern constraints; failures return 422 without echoing raw input. *Acceptance:* hypothesis-based fuzz on public endpoints yields no 500s.
- **TITAN-SEC-071 (MUST):** All database access uses SQLAlchemy parameterized queries; string-built SQL is banned (including generated migrations), enforced by a semgrep CI rule. *Acceptance:* rule active; sqlmap against the API finds nothing.
- **TITAN-SEC-072 (MUST):** All LLM-derived content in the dashboard is untrusted: React default escaping, no `dangerouslySetInnerHTML` for model content, markdown via a sanitizing pipeline (rehype-sanitize allowlist), CSP `default-src 'self'` without `unsafe-inline` scripts. *Acceptance:* stored-XSS payloads in agent output render inert; ESLint rule bans `dangerouslySetInnerHTML`.
- **TITAN-SEC-073 (MUST):** LLM tool-call arguments are schema-validated and constraint-checked before execution: paths canonicalized and confined to `/workspace`; shell arguments passed as argv arrays, never `sh -c`; URLs checked against allowlists. *Acceptance:* red-team suite with `../../etc/passwd`, `; rm -rf /`, and internal/metadata URLs fully rejected.
- **TITAN-SEC-074 (MUST):** Outbound agent fetches (Research Agent, webhook callbacks) pass an SSRF guard: resolved IPs checked against private/link-local/metadata ranges, redirects re-validated, http(s) only. *Acceptance:* SSRF corpus blocked and audited.
- **TITAN-SEC-075 (MUST):** Inbound webhooks verify HMAC signatures; unsigned or stale (> 5 min skew) deliveries rejected. *Acceptance:* unsigned webhook → 401; replay rejected.
- **TITAN-SEC-076 (MUST):** Error responses are generic (correlation ID only); stack traces never reach clients. *Acceptance:* forced exception returns sanitized body; detail only in server logs.

---

## 9. Rate Limiting and Abuse Prevention

- **TITAN-SEC-080 (MUST):** Redis-backed token-bucket limits, layered: per-IP pre-auth (60 req/min), per-user (600 req/min), per-org, plus stricter per-route limits on auth (10/min), project creation (10/hour), and LLM-triggering endpoints. 429 with `Retry-After`. *Acceptance:* load test observes limits ±5%; configurable per org.
- **TITAN-SEC-081 (MUST):** WebSocket controls: ≤ 10 concurrent connections per user, ≤ 20 msg/s per connection, backpressure then disconnect on sustained violation. *Acceptance:* flood test → disconnect + audit event.
- **TITAN-SEC-082 (MUST):** LLM budget caps per task, per project/day, per org/month (local models time-capped instead); breach pauses the task and notifies a Maintainer rather than silently truncating. *Acceptance:* runaway-loop simulation halts at cap with alert.
- **TITAN-SEC-083 (MUST):** Loop detection: ≥ N (default 3) near-identical consecutive tool calls, or step budget exceeded, pauses the task for review. *Acceptance:* seeded loop halts within N+1 iterations.
- **TITAN-SEC-084 (SHOULD):** Sandbox egress-proxy traffic rate-limited (default 100 requests/task) to blunt exfiltration. *Acceptance:* over-limit requests blocked and logged.

---

## 10. Audit Logging

### 10.1 Design

Append-only PostgreSQL table `audit_events` with INSERT-only grants for the application role (no UPDATE/DELETE at the DB privilege level), plus a hash chain (`event_hash = sha256(prev_hash || canonical_event_json)`) for tamper evidence. Events also stream to Loki for search and to object storage as daily sealed, age-encrypted segments.

### 10.2 Recorded Events

Authentication (success/failure, MFA, refresh/revocation, reuse detection); authorization denials; role/member changes; secret lifecycle (create/rotate/delete/inject — never values); agent task lifecycle; every tool call (agent, tool, parameter digest, allow/deny); approval-token mint/consume; sandbox create/destroy/policy exception; deploys; plugin install/config; memory-tier promotions; data export/deletion; scan results; redaction hits; rate-limit and SSRF blocks; incident-mode toggles.

- **TITAN-SEC-090 (MUST):** Each event records `{event_id, ts, actor, org, project, task, action, resource, outcome, source_ip, trace_id, event_hash}`; `trace_id` links to OpenTelemetry traces. *Acceptance:* catalog test exercises every category and asserts schema.
- **TITAN-SEC-091 (MUST):** The audit store is immutable to all application and human roles; a daily hash-chain verification job alerts on breaks. *Acceptance:* UPDATE/DELETE as app role fails at DB layer; corrupted row breaks verification and alerts.
- **TITAN-SEC-092 (MUST):** Audit events never contain secret values, full prompts, or code bodies — only references/digests; redaction (§6.4) applies. *Acceptance:* planted-secret action yields an event without the value.
- **TITAN-SEC-093 (MUST):** Retention defaults: 400 days online + 2 years sealed archive; org-configurable upward, downward only by Owner; legal-hold override. *Acceptance:* retention job tested on synthetic aged data.
- **TITAN-SEC-094 (SHOULD):** Admin-facing audit search (actor, action, resource, time) in the dashboard. *Acceptance:* seeded events found < 2 s at 1M rows.

---

## 11. Supply Chain Security and CI Scanning

### 11.1 Platform CI Gates

Every platform PR runs, in order: secret scan (gitleaks) → SAST (semgrep + bandit; ESLint security for TS) → dependency audit (pip-audit, npm audit, osv-scanner) → tests → container build → image scan (Trivy: CVEs, misconfig, licenses) → SBOM (Syft, CycloneDX) → image signing (cosign, keyless via CI OIDC).

- **TITAN-SEC-100 (MUST):** CI blocks merge on any detected secret, high+ SAST findings, or high/critical vulnerabilities without a recorded, expiring, Admin-approved exception. *Acceptance:* seeded vulnerable dep and planted secret each fail CI.
- **TITAN-SEC-101 (MUST):** Images built from digest-pinned bases, carry SBOMs, cosign-signed; production Kubernetes verifies signatures at admission (Kyverno/policy-controller). *Acceptance:* unsigned image rejected in prod cluster.
- **TITAN-SEC-102 (MUST):** All manifests use lockfiles with integrity hashes; CI installs are frozen-from-lockfile. *Acceptance:* lockfile drift fails CI.
- **TITAN-SEC-103 (SHOULD):** Weekly scheduled full-tree scans against fresh vulnerability feeds auto-open tracked issues. *Acceptance:* synthetic new CVE produces an issue.

### 11.2 Generated-Code Pipeline Gates

Generated project code passes the same gate classes inside its sandboxed pipeline (secret scan, SAST, dependency audit, tests) before Code Reviewer and Security Agent review, and before human merge approval (sequence wired in Volume 6).

### 11.3 Generated-Dependency Policy (T-AI-5)

- **TITAN-SEC-104 (MUST):** Before any agent-proposed dependency installs, the gate verifies: (a) package exists on the official registry (kills hallucinated packages); (b) age ≥ 30 days AND (weekly downloads ≥ 1000 OR Maintainer-allowlisted); (c) no high/critical vulnerabilities (osv-scanner); (d) name is not within Levenshtein distance 1 of a top-1000 package unless it is that package; (e) version pinned and hash-locked. Failures block install and open a review task. *Acceptance:* corpus of hallucinated, typosquatted, and vulnerable packages fully blocked; legitimate packages pass.
- **TITAN-SEC-105 (MUST):** Sandbox installs go only through the caching registry mirror (§5 proxy) — one enforcement and audit point. *Acceptance:* direct-registry install fails; mirror logs show installs.

---

## 12. OWASP Compliance Mapping

### 12.1 OWASP Top 10 (2021)

| OWASP | TITAN controls | Requirements |
|---|---|---|
| A01 Broken Access Control | Central RBAC middleware, object-level checks, agent allowlists, approval tokens | SEC-030..034 |
| A02 Cryptographic Failures | TLS everywhere, storage + field encryption, Argon2id, key rotation | SEC-017, 060..064 |
| A03 Injection | Pydantic validation, parameterized SQL, argv-only shell, sanitized rendering, CSP | SEC-070..073 |
| A04 Insecure Design | Living STRIDE + AI threat model, ADRs, defense in depth | SEC-003, 004 |
| A05 Security Misconfiguration | Secure-defaults checklist, PodSecurity restricted, CSP/HSTS | SEC-045, 060, §15 |
| A06 Vulnerable Components | CI dep/image scanning, lockfiles, weekly rescans, dependency gate | SEC-100..105 |
| A07 Identification & Auth Failures | OIDC+PKCE, MFA, rotation + reuse detection, throttling, WSS auth | SEC-010..017 |
| A08 Software & Data Integrity | Signed images + admission verification, signed envelopes, hash-chained audit | SEC-020, 091, 101 |
| A09 Logging & Monitoring Failures | Full audit catalog, Loki + OTel correlation, tamper evidence | SEC-090..094 |
| A10 SSRF | Egress-deny sandboxes, SSRF guard, metadata-IP blocklist | SEC-043, 074 |

### 12.2 OWASP LLM Top 10 (2025)

| OWASP LLM | TITAN controls | Requirements |
|---|---|---|
| LLM01 Prompt Injection | Untrusted-content tagging, model-independent tool authz, approval gates, egress deny | SEC-001, 031, 033, 043 |
| LLM02 Sensitive Information Disclosure | Redaction pipeline, secret-file exclusion, local-model routing | SEC-050..056 |
| LLM03 Supply Chain | Dependency gate, registry mirror, SBOM + signing | SEC-100..105 |
| LLM04 Data & Model Poisoning | Memory provenance tiers, namespace isolation, promotion review | §2.3; Vol. 5 |
| LLM05 Improper Output Handling | Output sanitization, tool-arg validation, sandbox-only execution | SEC-072, 073, 040 |
| LLM06 Excessive Agency | Per-agent allowlists, task-scoped tokens, approval tokens | SEC-018, 031, 033 |
| LLM07 System Prompt Leakage | No secrets in prompts by construction; prompts treated as non-secret config | SEC-053, 054 |
| LLM08 Vector & Embedding Weaknesses | Per-project Qdrant namespaces, authz on retrieval, provenance filtering | §2.3; Vol. 5 |
| LLM09 Misinformation | Reviewer/QA/Security agent pipeline, tests before merge, human approval | Vol. 6; SEC-033 |
| LLM10 Unbounded Consumption | Token budgets, loop detection, rate limits, sandbox caps | SEC-042, 080..084 |

- **TITAN-SEC-110 (MUST):** These mappings are kept as machine-readable traceability (`security/compliance-map.yaml`) checked in CI. *Acceptance:* removing a mapped test fails a CI consistency check.

---

## 13. Incident Response Runbook

### 13.1 Severity Levels

| Sev | Definition | Examples | Target |
|---|---|---|---|
| SEV-1 | Active compromise or secret exposure | Provider key leaked; sandbox escape; audit chain broken | Contain ≤ 1 h |
| SEV-2 | Exploitable weakness, no confirmed abuse | Auth bypass; injection reaching tool layer | Mitigate ≤ 24 h |
| SEV-3 | Control failure without direct exposure | Scanner outage; redaction false-negative class | Fix ≤ 7 d |

### 13.2 Phases

1. **Detect & report:** alerts from audit anomalies and Prometheus rules (auth-failure spikes, egress-proxy denials, budget breaches, chain-verification failure); `SECURITY.md` intake for external reporters with 90-day coordinated disclosure.
2. **Triage:** on-call Maintainer assigns severity, opens a timestamped, immutable incident record.
3. **Contain:** one-command lockdown — `titan incident lockdown` pauses all agent execution, freezes deploys, revokes agent service tokens, forces privileged re-auth. Targeted: revoke token family, kill sandbox, disable plugin, rotate secret.
4. **Eradicate & recover:** rotate affected credentials, patch, redeploy from signed images, verify audit chain, restore from encrypted backups if integrity is in doubt.
5. **Post-incident:** blameless review within 7 days; outcomes become new requirements and regression tests; user notification per disclosure policy.

### 13.3 AI-Specific Playbooks

- **P1 Prompt-injection incident:** quarantine offending content; replay the task trace (trace_id) to map influenced tool calls; revert downstream artifacts; add exploit to red-team corpus.
- **P2 Malicious generated code merged:** locate via artifact digests which branches/deployments contain it; auto-open revert PRs; rescan history; review the gate miss.
- **P3 Secret in LLM context:** rotate immediately (assume compromised — provider logs are outside our control); audit which providers received it; strengthen redaction rule; notify org Admins.
- **P4 Memory poisoning:** freeze tier promotions; query memory by provenance for records from the tainted actor/window; purge and rebuild derived embeddings.

- **TITAN-SEC-120 (MUST):** Lockdown and all four playbooks are implemented and rehearsed via game-days twice yearly. *Acceptance:* staging lockdown drill halts agents ≤ 60 s; drill reports archived.
- **TITAN-SEC-121 (MUST):** The repo contains `SECURITY.md` with reporting channel, SLAs, disclosure and supported-version policy. *Acceptance:* file present, linked from README.

---

## 14. Security Testing

### 14.1 SAST and Static Controls (every PR)

semgrep with a custom TITAN ruleset (no string SQL, no `sh -c`, no `dangerouslySetInnerHTML`, no unvalidated tool-arg use), bandit, ESLint security + strict TypeScript, gitleaks, IaC scanning (Trivy config/Checkov on K8s manifests and Compose files).

### 14.2 DAST (nightly, staging)

OWASP ZAP baseline + authenticated full scan against the staging API and dashboard; schemathesis fuzzing from the OpenAPI schema. Findings ≥ high auto-open blocking issues.

### 14.3 Penetration Test Scope

Annual and pre-1.0: external web/API surface; multi-tenant abuse (cross-org IDOR); WebSocket layer; **sandbox escape from inside an agent sandbox** (explicitly in scope, via a harness task granting agent-equivalent code execution); egress-proxy bypass; queue message forgery; vault interface. Out of scope: third-party LLM provider infrastructure; DoS beyond rate-limit verification.

### 14.4 LLM Red-Teaming (continuous)

- **TITAN-SEC-130 (MUST):** A red-team corpus of injection and tool-abuse payloads (seeded from public suites such as garak/PyRIT plus TITAN-specific cases: injected README, malicious issue text, poisoned memory record, hostile web page for the Research Agent) runs weekly in CI against a staging orchestrator with a real gateway and mock destructive tools. Pass: zero unauthorized tool executions, zero secret-bearing gateway payloads. *Acceptance:* pipeline publishes a scorecard and blocks release on regression.
- **TITAN-SEC-131 (MUST):** Every incident and pen-test finding becomes a permanent regression test. *Acceptance:* traceability from findings register to test IDs.
- **TITAN-SEC-132 (SHOULD):** Coverage-guided fuzzing on the highest-risk parsers (tool-call argument parser, webhook parser, envelope verifier). *Acceptance:* fuzz targets exist with CI time-box.

---

## 15. Secure Defaults Checklist

Every fresh install ships with these defaults; a built-in `titan security check` command verifies them and prints a scorecard.

| # | Default | Ref |
|---|---|---|
| 1 | Sandbox egress: deny-all (proxy allowlist only) | SEC-043 |
| 2 | Auto-deploy and auto-merge OFF (human approval required) | SEC-033 |
| 3 | Services bind loopback/internal networks; only UI/API exposed | SEC-060 |
| 4 | First admin: forced password change + MFA prompt at first login | SEC-015, 017 |
| 5 | TLS on all non-loopback listeners; HSTS on public endpoints | SEC-060 |
| 6 | JWT RS256/EdDSA, 15-min expiry; refresh rotation on | SEC-011, 012 |
| 7 | SOPS+age vault initialized; secret-file patterns excluded from context/RAG | SEC-050, 055 |
| 8 | Redaction pipeline on gateway, logs, sandbox output | SEC-054 |
| 9 | Rate limits and LLM budget caps on, conservative defaults | SEC-080, 082 |
| 10 | Audit log on: INSERT-only grants, hash chain active | SEC-090, 091 |
| 11 | Containers non-root, read-only rootfs, caps dropped, limits set | SEC-041, 042 |
| 12 | Security gates enabled by default in generated projects' pipelines | SEC-100, 104 |
| 13 | Memory: per-project namespaces; cross-project retrieval off until promotion | §2.3 |
| 14 | Generic errors + correlation IDs (detail suppression on) | SEC-076 |
| 15 | Prometheus/Grafana/Loki endpoints auth-protected, not public | SEC-060; Vol. 9 |

- **TITAN-SEC-140 (MUST):** `titan security check` exists, verifies all rows, exits non-zero on failure; CI runs it against the Compose profile. *Acceptance:* seeded misconfiguration is detected.

---

## 16. Risks and Open Questions

| ID | Risk / Question | Position |
|---|---|---|
| R-1 | Prompt injection is not fully solvable; soft controls will be bypassed. | Accepted: architecture assumes bypass and leans on hard controls (SEC-001/031/033/043/054). Residual risk: manipulated-but-authorized actions within an agent's legitimate scope; monitored via red-team scorecard trend. |
| R-2 | gVisor overhead/compatibility may frustrate heavy builds. | Per-project runtime-class override (Admin-approved, audited); Firecracker evaluation deferred to Volume 10 timeframe. |
| R-3 | Redaction false negatives (novel key formats, secrets split across chunks). | Entropy + inventory hashing reduce but cannot eliminate; P3 playbook covers escapes. Open: chunk-boundary-aware scanning cost. |
| R-4 | Laptop deployments cannot enforce mTLS/gVisor; isolation is weaker. | Loopback binding, egress-deny proxy, identical authz code paths; documented honestly in install docs. |
| R-5 | Dependency-gate thresholds may block legitimate niche packages. | Maintainer allowlist path; tune thresholds from telemetry. Open: per-language calibration. |
| R-6 | Postgres hash chain is tamper-evident, not tamper-proof, against a DB superuser. | Daily sealed off-store archives bound exposure to ≤ 24 h. Open: external timestamp anchoring — deferred. |
| R-7 | Provider-side prompt retention (TB4) is outside TITAN's control. | Local-model routing (SEC-056) is the strong answer; document per-provider retention settings in gateway drivers. |
| Q-1 | Should approval tokens require WebAuthn step-up? | Leaning yes for production 1.0; SHOULD-level in a future revision. |
| Q-2 | Multi-tenant SaaS hosting needs per-tenant sandbox node isolation review. | Out of MVP scope; flagged for Volume 10 enterprise track. |

---

## 17. Cross-References

- **Volume 1 — Foundation:** canonical stack decisions and non-functional requirements satisfied here.
- **Volume 2 — Multi-Agent Intelligence:** Security Agent role spec; agent authority constrained by §4.3.
- **Volume 3 — Backend Engineering:** FastAPI middleware ordering; Redis Streams topology carrying §3.5 envelopes.
- **Volume 4 — Frontend:** dashboard surfaces for audit search, secret rotation, approvals, security scorecard.
- **Volume 5 — AI Brain:** memory tiers, provenance metadata, and namespace isolation referenced by T-AI-6, LLM04, LLM08.
- **Volume 6 — Engineering Workflow:** placement of review/approval gates (SEC-033, §11.2) in the idea-to-release pipeline.
- **Volume 7 — Integrations:** plugin permission model and webhook handling governed by SEC-075 and §4.2.
- **Volume 9 — Production:** monitoring/alerting feeding §13 detection; encrypted backups (SEC-062); CI/CD hosting §11 gates.
- **Volume 10 — Future:** marketplace agent vetting, enterprise isolation (Q-2), evolution guardrails built on this containment model.

*End of Volume 8 — Security Specification (TITAN-VOL-08, Draft v1.0).*
