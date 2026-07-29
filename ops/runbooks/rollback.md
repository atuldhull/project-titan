# RUNBOOK-02 — Rollback

> Vol 9 §5.2 / §12 (TITAN-OPS-025). Skeleton: **Preconditions → Steps → Verification →
> Escalation → Post-actions**. Target: rollback in **< 5 min** via a Service-selector
> flip back to the last-known-good color (ADR-OPS-1).
>
> **Last drilled:** _not yet drilled — pre-first-launch. Blue/green flip + rollback is exercised in staging each cycle (readiness checklist)._

## Preconditions

- `kubectl` context on the target cluster (`ops/scripts/rollback.sh` operates on it), or Compose access for a local stack.
- The previous (last-known-good) color is identified and its pods are still Ready — confirm with `ops/scripts/rollback.sh status` before flipping.
- Authority to flip the Service selector and to quarantine a digest; the incident channel is open if this is user-visible.
- You know whether the failing release included a **contract** (destructive) migration — if so, a flip is unsafe (see the decision matrix).

## Decision matrix

| Situation | Path |
|---|---|
| Bad release, old color still warm (within/just after the 30-min window) | **Selector flip back** — fastest, `rollback.sh all` |
| Old color already scaled to zero | **Redeploy** last-good digest, then flip |
| Failure is data/migration-shaped (not the image) | Do **not** just flip — go to **[RUNBOOK-04](backup-restore.md)** / **[RUNBOOK-03](incident.md)** |
| Automated burn-rate rollback already tripped | Verify it landed; confirm recovery below |

**Key rule:** blue/green migrations are expand/contract, so the previous version is always
schema-compatible with the current DB — a flip back is safe. If someone shipped a
**contract** (destructive) migration, a flip is **not** safe; treat as an incident.

## Steps

1. **Flip back to last-known-good:**
   ```bash
   ops/scripts/rollback.sh all          # flips every Service selector back to the previous color
   ```
2. **Verify the selector flipped** (no pod restart needed — the old color is already running):
   ```bash
   ops/scripts/rollback.sh status       # prints active color per service
   ```
3. **Watch SLO recovery** — error-budget burn should return **< 1×** within minutes.
4. **Quarantine the bad digest** (records it in-cluster; automatic refusal-to-schedule
   lands with the Phase-79 admission policy, so until then treat it as a manual "do not
   re-promote" marker):
   ```bash
   ops/scripts/rollback.sh quarantine <bad-sha256-digest>
   ```
5. **Re-enable paused feature flags only after the cause is known** — never blanket-re-enable.

## Verification

- Active color is the previous good build (`rollback.sh status`).
- Error-budget burn back < 1×; the firing page clears; p95 back to baseline.
- The quarantined digest is recorded and blocked from re-promotion.

## Escalation

- **Rollback ineffective** (recovery doesn't come, or the fault is in shared datastores) →
  declare an incident (**[RUNBOOK-03](incident.md)**), consider `agents.pause_all`, and assess
  whether data restore (**[RUNBOOK-04](backup-restore.md)**) is needed.
- Rollback blocked by a contract migration → this is a SEV-1 (the schema moved under you).

## Post-actions

- **Mandatory blameless post-mortem within 48 h** of any production rollback.
- The previous (bad) digest stays **quarantined** until the root cause is fixed and re-reviewed.
- File action items as issues; update the deploy runbook if a gap let the bad release through.
