# RUNBOOK-03 — Incident Response

> Vol 9 §12 (TITAN-OPS-025). Skeleton: **Preconditions → Steps → Verification →
> Escalation → Post-actions**. This runbook is the landing page for **every paging
> alert**: a page is the first minute of an incident. The alert-specific sections at
> the bottom (`## API Fast Burn`, `## Postgres Lag`, …) are the anchors that each
> Prometheus `runbook_url` resolves to — keep those headings stable.
>
> **Last drilled:** _not yet drilled — pre-first-launch (see `ops/templates/prod-readiness.md`)._

---

## SEV definitions

| SEV | Definition | Example | Update cadence |
|---|---|---|---|
| **SEV-1** | Platform down, or confirmed/likely data loss | API 5xx > 50%, Postgres primary lost, audit chain broken | every 30 min |
| **SEV-2** | SLO breach or a major feature down, no data loss | fast error-budget burn, worker crash-loop, provider fully degraded | every 60 min |
| **SEV-3** | Degraded but serving | slow burn, queue backpressure draining, latency elevated | at resolution |

## Roles (declare within the first 5 minutes)

- **Incident Commander (IC)** — owns the decision to roll back / declare SEV / page others. Not hands-on-keyboard.
- **Operator** — runs the commands (deploys, kill switches, restores). One person, so actions serialize.
- **Comms** — updates the status page and the incident channel; shields IC/Operator from questions.

For a solo self-hoster, one person holds all three roles; the documented solo-operator
mode in the readiness checklist applies (announce, then act, then record).

## Preconditions

- `kubectl` / `docker compose` access to the affected environment.
- Grafana open on **Platform Overview** (`titan-health`) and **SLO** (`titan-slo`).
- The kill-switch CLI reachable: `titan flags set …` and `titan incident lockdown` (Phase 72/75).

## First 10 minutes (Steps)

1. **Acknowledge** the page in Alertmanager/ntfy so a second page doesn't wake a second person.
2. **Open** the incident channel + status page (Comms). Name an IC.
3. **Look at Platform Overview**: is this one signal or many? Note the blast radius.
4. **Check recent deploys** (deploy annotation on the overview dashboard):
   - Deploy in the last 30 min? → this is a bad release → **[RUNBOOK-02 Rollback](rollback.md)**.
   - No deploy? → follow the alert-specific section below.
5. **Reach for a kill switch** if the cause is a class of work, not a bad build:
   - Runaway agents / spend / audit anomaly → `titan flags set agents.pause_all on`
     (or the full `titan incident lockdown` for a suspected compromise — Phase 75).
   - Intake overwhelming the tier → `titan flags set intake.read_only on` (keeps ledgering, sheds execution).
   - One LLM provider is the problem → `titan flags set llm.provider_override on --value <fallback>`.
6. **Set a timer** for the SEV update cadence above.

## Kill-switch inventory

| Switch | Effect | Fail direction |
|---|---|---|
| `agents.pause_all` | Halts the live dispatch loop, reaper, and inbox — no new agent work | **closed** (nothing runs) |
| `intake.read_only` | Intake returns 503 for writes; ledger reads/status stay up | **open** (still accepts status) |
| `llm.provider_override` | Forces the gateway to a chosen provider/fallback | provider-scoped |
| `titan incident lockdown` | pause_all + intake read-only + token-epoch bump + **revoke_all_sessions** (Phase 75) | **closed** |

## Verification

- The firing alert clears in Alertmanager and the driving panel returns to baseline.
- No **new** alert fired as a side effect of the mitigation (e.g. a kill switch didn't starve a healthy tier).

## Escalation

- Mitigation ineffective within the SEV cadence → raise the SEV one level and page the next on-call.
- Rollback didn't recover → keep SEV-1, consider `agents.pause_all`, and begin DR assessment (**[RUNBOOK-04](backup-restore.md)**).

## Post-actions

- Reconstruct the timeline from Loki (logs) and Tempo (traces).
- Blameless post-mortem within 48 h for SEV-1/SEV-2; action items filed as issues.
- Re-enable any flags flipped, one at a time, only after the cause is understood.

---

# Alert response index

Each section below is the anchor a paging alert links to. Format per alert:
**what it means → immediate checks → mitigation → when to escalate.**

## API Fast Burn

The availability error budget is burning ≥ 14.4× (1h **and** 5m windows) — a large
fraction of requests are failing right now.
- **Check:** Platform Overview 5xx panel; recent deploy annotation; gateway/provider health.
- **Mitigate:** deploy in the last 30 min → **[roll back](rollback.md)**. No deploy → identify the failing dependency (Postgres? provider? see below) and apply its section.
- **Escalate:** SEV-2 immediately; SEV-1 if 5xx > 50% or climbing.

## API Slow Burn

The budget is burning ≥ 3× (6h **and** 30m) — a persistent, lower-grade failure rate
that will exhaust the monthly budget if left.
- **Check:** which routes/status codes dominate (Loki); is it one endpoint or broad?
- **Mitigate:** open a ticket-severity investigation; fix forward. No emergency flip unless it accelerates into fast burn.
- **Escalate:** SEV-3; upgrade if it crosses into fast burn.

## API Latency

p95 request duration is over its SLO threshold.
- **Check:** DB saturation (see **Postgres Lag**), queue depth, gateway latency, CPU throttling on api pods.
- **Mitigate:** scale api if CPU-bound; if a slow query, capture it from `pg_stat_activity`; shed load with `intake.read_only` only if the API itself is the bottleneck.
- **Escalate:** SEV-3, or SEV-2 if latency breaches user-facing timeouts.

## Queue Backpressure

Oldest pending stream entry exceeds the backpressure threshold — workers can't keep up.
- **Check:** worker replica count and XPENDING lag (Data Stores dashboard); is a worker crash-looping (below)?
- **Mitigate:** scale workers (KEDA should, verify it is); if intake is the firehose, `intake.read_only` sheds new writes while the ledger stays durable. Queue loss is **recoverable** — `ops/scripts/requeue-from-ledger.py` rebuilds from the ledger.
- **Escalate:** SEV-3; SEV-2 if lag grows unbounded after scaling.

## Worker Crashloop

Workers are restarting repeatedly (CrashLoopBackOff / restart-rate alert).
- **Check:** `kubectl logs --previous` (or `docker compose logs worker`) for the panic; a poison task? a bad migration? a missing secret?
- **Mitigate:** bad release → **[roll back](rollback.md)**. Poison task → identify and park it; `agents.pause_all` if the loop is burning budget.
- **Escalate:** SEV-2 (execution tier down).

## Postgres Lag

Replication lag (or replica health) alert on the primary/replica pair.
- **Check:** `postgres_exporter` lag metric; is the replica up; WAL disk pressure; a long transaction holding WAL.
- **Mitigate:** kill the offending long transaction; ensure WAL archiving to object storage is keeping up (a stuck archiver fills the disk — see **Backup Failed**). Do **not** fail over on lag alone.
- **Escalate:** SEV-2; SEV-1 if the primary is at risk of a full WAL disk.

## Redis Memory

Redis memory approaching `maxmemory` with `noeviction` (queue durability requires no eviction).
- **Check:** stream length (XLEN) vs the MAXLEN cap; is a consumer group stuck not ACKing.
- **Mitigate:** unstick or scale the lagging consumer; trim only after confirming entries are ledgered. Never enable eviction on the queue instance.
- **Escalate:** SEV-2 (risk of write refusal on the queue).

## Provider Degraded

An LLM provider's error ratio is ≥ its degradation threshold (errors ÷ total attempts).
- **Check:** LLM Spend dashboard `titan_llm_errors_total{provider}`; is it one provider or all?
- **Mitigate:** `titan flags set llm.provider_override on --value <fallback>` (Ollama fallback is always legal); results tag `degraded_model=true` for stricter review.
- **Escalate:** SEV-2 if all providers degraded and no fallback is healthy.

## Budget Exhaustion

A project hit 100% of its LLM budget — tasks are entering `budget_hold`.
- **Check:** which project (Spend dashboard); is this expected spend or a runaway loop?
- **Mitigate:** legitimate → owner raises the budget (tasks resume without loss). Runaway → `agents.pause_all` and inspect the LoopGuard; do not just raise the budget.
- **Escalate:** SEV-3; SEV-2 if it correlates with a spend anomaly.

## Spend Anomaly

Global spend rate is anomalously high versus the rolling baseline.
- **Check:** which project/agent dominates the burn; a loop or a prompt-size regression.
- **Mitigate:** `agents.pause_all` to stop the bleed, then find the driver before resuming. Consider tightening the per-task cap.
- **Escalate:** SEV-2 (financial impact) — this is a page for a reason.

## Cert Expiry

A TLS certificate is within its expiry warning window.
- **Check:** which host/secret; is cert-manager renewal failing (look at the Certificate/Order events).
- **Mitigate:** unblock the ACME challenge (DNS/HTTP-01) or rotate the secret manually; force a renewal.
- **Escalate:** SEV-2 if < 24 h to expiry with no successful renewal.

## Audit Chain Broken

The daily audit-chain verify found a hash discontinuity (Phase 73) — tamper-evidence tripped.
- **Check:** the `AuditChainBroken` detail; which sequence range; the last valid seal.
- **Mitigate:** treat as a **potential compromise** — `titan incident lockdown` (revokes all sessions + freezes intake), preserve the table, do **not** truncate or "repair" the chain.
- **Escalate:** **SEV-1** — this may be data tampering; engage security and the DR path if integrity can't be re-established from a seal.

## Agent KPI Degraded

The nightly evolution KPI rollup flagged a sustained agent-KPI drop (`AgentKpiDegraded`,
Phase 84 / TITAN-EVO-007): an agent/task_class composite fell >10% below its trailing-30-day
mean. An `evolution.investigation` record for the Memory Agent is opened automatically with the
KPI series snapshot — this is a **quality regression, not an outage** (ticket, not page).
- **Check:** which `{agent, task_class}`; open the investigation (`GET /api/v1/evolution/investigations`)
  and its `series_snapshot` (recent vs baseline); check whether a recent Evolution Proposal changed
  the agent's prompt/routing/spec (`GET /api/v1/evolution/agents/{agent}/kpis`).
- **Mitigate:** if a merged EP caused it, `git revert` the evolution commit + redeploy (restores the
  prior approved prompt bytes); otherwise let the Memory Agent's investigation run its course.
- **Escalate:** SEV-3 — track the investigation to closure; no live-traffic impact.

## Evolution Subsystem Failing

More than half of the post-terminal evolution operations (scorecard / reflection / failure
classification / RCR) errored over 30 minutes (`EvolutionSubsystemFailing`, Phase 85). The whole
subsystem is fail-safe by design — a broken prompt or provider degrades to `F-UNK` / no reflection
rather than blocking task closure — so without this signal it would be an **invisible silent no-op**
(the consumer keeps ACKing; queue depth stays 0). No live-traffic impact, but the evolution loop
(scorecards, reflections, RCRs) is not learning.
- **Check:** `titan_evolution_ops_total{outcome="failed"}` by `op` to see which stage fails; the
  most common cause is an evolution registry prompt that is unapproved/drifted/missing —
  run `uv run python scripts/check_evolution.py` (the same CI gate) and check the LLM provider
  health (a provider outage degrades every rubric/reflection/classify call).
- **Mitigate:** re-pin/re-approve the drifted prompt (Phase-83 EP flow) or restore provider access;
  the subsystem self-heals on the next terminal task (steps are idempotent — no backfill needed).
- **Escalate:** SEV-3 — quality/learning regression, not an outage.
