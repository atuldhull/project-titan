# DR drill reports (Vol 9 §8.3, TITAN-OPS-019)

`ops/scripts/dr-drill.sh` rehearses recovery from **total cluster loss** and writes a timestamped
`dr-<epoch>.json` report here. `apps/api/scripts/check_dr_report.py` (`make check-dr-report`)
validates it so a drill can never be quietly marked PASS while a step was skipped or the recovery
blew the budget (the Phase-78 "decorative verification" lesson: a drill that *logs* success proves
nothing — the report must show every step completed and the MEASURED recovery inside the target).

## Restore order (§8.3) — the report's `steps`, in sequence

1. **provision** — cluster from `infra/` (Terraform + kustomize)
2. **restore-secrets** — the sealed secrets bundle into the vault
3. **restore-postgres-pitr** — WAL point-in-time recovery to latest (drives the measured RPO)
4. **requeue-from-ledger** — start Redis empty, re-enqueue non-terminal ledger rows
   (`ops/scripts/requeue-from-ledger.py`)
5. **restore-qdrant** — vector snapshots
6. **deploy-lkg-digests** — last-known-good image digests from the release manifest
7. **smoke** — the smoke suite green in the restored cluster
8. **flip-dns** — point DNS at the rebuilt ingress (no user traffic until the stack is proven)

## Targets

**RTO ≤ 4 h** (total wall time) and **RPO ≤ 5 min** (data-loss window = the gap to the newest
replayable WAL). The gate fails the report if either is exceeded.

## Running

```bash
ops/scripts/dr-drill.sh --dry-run                    # verify wiring + report contract; touches NO infra
TITAN_DR_DRILL_CONFIRM=yes ops/scripts/dr-drill.sh   # the real, DESTRUCTIVE drill (staging only)
make check-dr-report                                 # validate the newest report
```

## Verified vs. staging-only

- **Verified locally + in CI:** `bash -n`, a `--dry-run` that runs the full 8-step sequence with
  infra actions skipped and emits a report, and `check_dr_report.py` accepting it (+ its re-break
  tests). This proves the step order and the RTO/RPO report contract.
- **Staging-only (destructive, needs a cluster + backups):** the real drill that provisions,
  destroys, and rebuilds. It runs on the release-candidate cycle in staging; the report it
  produces is validated by the same gate. `dr-*.json` run artifacts are git-ignored here.
