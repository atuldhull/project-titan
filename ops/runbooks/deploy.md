# RUNBOOK-01 — Deploy

> Vol 9 §3 / §5.1 / §12 (TITAN-OPS-006/025). Skeleton: **Preconditions → Steps →
> Verification → Escalation → Post-actions**. Blue/green with a Service-selector flip
> and a 30-minute observation window (ADR-OPS-1).
>
> **Last drilled:** _not yet drilled — pre-first-launch._

## Preconditions

- All §3.4 deployment gates green on the release commit: tests + coverage, migration-compat
  job (OPS-013), image build + **cosign signature on the digest** (OPS-005), SBOM + CVE scan.
- Migration (if any) follows **expand/contract** and passed the previous-version compatibility job.
- The production-readiness **delta items** for this release are checked (`ops/templates/prod-readiness.md`).
- New risky paths sit behind feature flags defaulting **OFF**, owners assigned.

## Steps

1. **Tag & trigger.** Push the release tag; `release.yml` runs the §3.4 gates, then waits on
   the environment's required-reviewer approval.
   ```bash
   git tag -s vX.Y.Z -m "release vX.Y.Z" && git push origin vX.Y.Z
   ```
2. **Migrate (expand).** The `titan-migrate` pre-deploy job applies additive migrations under
   the advisory lock (Phase 76). Confirm it succeeded before any pod flips.
3. **Roll out the idle color (green).** The pipeline deploys green at the signed digests
   (never `:latest`). Watch pods reach Ready — readiness gates on DB/Redis health, so a bad
   config **stalls** here instead of taking traffic.
4. **Smoke on the preview URL.** The pipeline runs the smoke suite against green's preview
   service before any user traffic. Confirm green in the run.
5. **Approve the flip.** With smoke green, approve the environment gate. The Service selector
   flips atomically from blue → green.
6. **Watch the SLO burn panel for 30 minutes** (SLO dashboard). Deploy annotation should show
   on Platform Overview; error rate and p95 flat.
7. **Scale down the old color** once the window is clean; the idle color scales toward zero
   between releases (2× capacity is only held during the window — ADR-OPS-1).

## Verification

- Deploy annotation visible on Platform Overview; 5xx and p95 flat across the 30-min window.
- No burn-rate alert fired during observation; the new version's metrics report the expected build.
- Contract phase of the migration (dropping the old column/shape) is scheduled for a **later**
  release, not this one.

## Escalation

- **Any fast-burn during the window → [RUNBOOK-02 Rollback](rollback.md) immediately.** Do not
  debug forward under a firing page.
- Rollout stalls at readiness (pods never Ready) → the config/dependency is bad; fix and re-run
  the pipeline — no flip happened, so users are unaffected.

## Post-actions

- Update release notes; close the release issue with links to the green CI runs.
- Re-verify the readiness delta items are all checked in the release issue.
- If flags were introduced OFF, schedule their enablement + eventual cleanup (stale-flag gate, OPS-012).
