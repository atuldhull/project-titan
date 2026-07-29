---
name: Production Readiness
about: Gate the FIRST production launch (and re-verify delta items each release) — Vol 9 §11, TITAN-OPS-024
title: "Production readiness — <release tag>"
labels: ["release", "readiness"]
---

# Production Readiness Checklist (TITAN-OPS-024)

Complete **100%** before the first production launch; re-verify the **delta** items each
release. Every box is either **checked with a link to evidence** (CI run, drill report,
dashboard) or **marked as a documented gap with a reason and an owner**. No first launch
without two maintainer sign-offs (or the documented solo-operator mode below).

> **Legend:** ✅ met · ⛔ documented gap (reason + owner) · ▶ re-verify each release (delta item).
> The pre-filled status is the **pre-first-launch assessment as of Phase 78 (M7 complete)**.
> M8 (Phases 79–80: Kubernetes, HA, autoscaling, NetworkPolicies) is optional post-MVP —
> items that depend on it are honest gaps until that infra lands, not silent blanks.

## Build & supply chain
- [x] ✅ All images digest-pinned, cosign-signed, SBOM published — `main.yml` signs the **digest**; `check_deploy_digests.py` gates floating tags (Phase 76).
- [x] ✅ ▶ CVE scan clean or waivered per Volume 8 — `dependency-audit` + nightly Trivy re-scan; exceptions in `security/scan-exceptions.yaml` (Phase 74).
- [ ] ⛔ Reproducible build verified for the release tag — *gap: reproducibility harness not yet built (owner: release eng; post-MVP).* 

## Deploy
- [ ] ⛔ ▶ Blue/green flip and rollback exercised in staging this cycle — *gap: needs a live staging K8s cluster (Phase 79). `ops/scripts/rollback.sh` + RUNBOOK-02 are ready; unexercised against real infra.*
- [x] ✅ ▶ Migration follows expand/contract and passed the previous-version compatibility job — `check_migrations.py` hard-gates ACCESS EXCLUSIVE + real downgrades (Phase 76).
- [x] ✅ ▶ Feature flags for all new risky paths default OFF, owners assigned — self-hosted flags + stale-flag gate (Phase 72).

## Resilience
- [ ] ⛔ Probes defined and validated (kill-pod drill) — *gap: liveness/readiness/startup probes land with the K8s manifests (Phase 79).*
- [ ] ⛔ PDBs, anti-affinity, HPA/KEDA configured and load-tested — *gap: HA + autoscaling are Phases 79–80 (optional M8).*
- [ ] ⛔ ▶ Backpressure test (TITAN-OPS-020) green — *partial: `ops/k6/intake-backpressure.js` exists; the 5×-capacity/30-min run needs the staging cluster (Phase 79).*

## Data
- [x] ✅ Backups running and restore-verified (TITAN-OPS-018) — Compose `backup` sidecar ON by default; nightly `backup-verify` restores base+WAL into a scratch DB (Phase 78).
- [ ] ⛔ ▶ PITR tested within 90 days — *gap: not yet drilled (pre-first-launch). `make pitr-drill` drives the full round trip but needs staging Postgres+WAL+MinIO; run it and record the drill date here before launch.*
- [x] ✅ Task-ledger reconciliation job green — `ops/scripts/reconcile-ledger.sh` (post-restore) + the chaos reconciliation invariant (Phase 55).

## Observability
- [x] ✅ Five dashboards live — provisioned as code, asserted by `test_dashboards.py` (Phase 77).
- [ ] ⛔ ▶ All Section 7 alerts loaded and **test-fired** — *partial: rules validated by `check_alert_rules.py`; synthetic-breach firing needs a running Prometheus/Alertmanager (staging).* 
- [x] ✅ Runbook links resolve — `check_runbook_links.py` proves every alert `runbook_url` resolves to an `ops/runbooks` anchor (Phase 78).
- [ ] ⛔ Trace continuity verified (TITAN-OPS-016) — *gap: end-to-end trace assertion needs the staging otel pipeline (Phase 79).*

## Security (verify with Volume 8)
- [ ] ⛔ NetworkPolicies default-deny verified — *gap: NetworkPolicies are Phase 79 (K8s). Local egress is enforced by Compose network topology today.*
- [x] ✅ Secrets from external secret store, none in env-committed files — age vault + KMS-sourced keys; `local-age.key` is gitignored, boot refuses empty crypto keys (Phases 68/78).
- [ ] ⛔ TLS everywhere — *gap: cert-manager/ingress TLS lands with K8s (Phase 79); `CertExpirySoon` alert is wired.*
- [x] ✅ RBAC audit complete — scope/role model + immutable audit chain (Phases 67/73).

## Cost
- [x] ✅ ▶ Budgets configured for every active org/project — per-project budgets enforced pre-call at the gateway, hard stop at 100% (Phase 71).
- [x] ✅ Spend-anomaly alert live — `GlobalSpendAnomaly` page (Phase 77).
- [ ] ⛔ Ollama fallback verified (TITAN-OPS-022) — *partial: fallback ladder is implemented + config-driven; the "cloud keys revoked → completes on Ollama" staging test needs the cluster.*

## Process
- [x] ✅ On-call rotation staffed (min 2) **or** documented solo-operator mode — see below.
- [x] ✅ SEV definitions and escalation path published — RUNBOOK-03 (`ops/runbooks/incident.md`).
- [ ] ⛔ DR drill within last quarter — *gap: not yet drilled (pre-first-launch). `make pitr-drill` is the automated drill; record the first drill date at launch.*

---

## Solo-operator mode (self-hosters)

TITAN is designed to be run by one person. When a 2-person on-call rotation isn't
available, the solo operator MAY sign off both slots **provided**:

- The kill switches (`agents.pause_all`, `intake.read_only`, `llm.provider_override`) and
  `titan incident lockdown` are verified reachable from the operator's environment.
- `BackupFailed`, `AuditChainBroken`, and `GlobalSpendAnomaly` pages route to a channel the
  operator actually receives out-of-hours (ntfy/email), tested once.
- The **announce → act → record** discipline from RUNBOOK-03 is followed (no silent changes),
  since there is no second pair of eyes.

This mode is documented per TITAN-OPS-024's "documented solo-operator mode for self-hosters"
and per risk R5's mitigation (backups default ON).

## Sign-off (TITAN-OPS-024 — two maintainers)

- [ ] Maintainer 1: `@___________`  — date: `______`  — evidence reviewed: ✅
- [ ] Maintainer 2: `@___________`  — date: `______`  — evidence reviewed: ✅
- [ ] (Solo-operator mode invoked — single maintainer, conditions above met): `@___________`

**Launch is blocked until every box above is ✅ or an accepted ⛔ gap, and the sign-offs are complete.**
