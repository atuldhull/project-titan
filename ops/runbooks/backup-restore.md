# RUNBOOK-04 — Backup & Restore

> Vol 9 §8.2 / §8.3 / §12 (TITAN-OPS-018/019/025). Skeleton: **Preconditions → Steps
> → Verification → Escalation → Post-actions**. The `## Backup Failed` heading is the
> anchor the `BackupFailed` page resolves to — keep it stable.
>
> **Last drilled:** _not yet drilled — pre-first-launch. `make pitr-drill` is the automated drill (staging/CI)._

## RPO / RTO targets (§8.3)

| Store | RPO | RTO | True recovery of record |
|---|---|---|---|
| PostgreSQL | ≤ 5 min (WAL PITR) | ≤ 1 h | base + WAL replay |
| Qdrant | ≤ 6 h (snapshot) | ≤ 2 h | re-embed from Postgres (Vol 5) |
| Redis | ≤ 1 h nominal / **zero task-loss via ledger** | ≤ 15 min | `requeue-from-ledger.py` |
| Full-platform (cluster loss) | ≤ 5 min (system-of-record) | ≤ 4 h | full DR sequence below |

## Preconditions

- Object-store (MinIO/S3) credentials available; the `backup` sidecar has been shipping (`docker compose ps backup`).
- `ops/scripts/{backup,restore}.sh` present; the KMS/age key for decrypting backups available.
- For any write-affecting restore: authority to flip `intake.read_only`.

## Verify a backup (Steps)

```bash
# Nightly job status (CI) or the sidecar's last run:
docker compose logs --tail=50 backup
# List the encrypted artifacts and confirm freshness (base ≤ 24h, WAL continuous):
ops/scripts/restore.sh list
```

## Single-table recovery (surgical, no downtime)

```bash
# 1. PITR-restore into a THROWAWAY clone DB at a time just before the mistake:
ops/scripts/restore.sh pitr --target-time "2026-07-29T13:40:00Z" --into titan_clone
# 2. Dump only the affected table from the clone:
pg_dump --data-only --table=public.<table> "$CLONE_URL" > /tmp/<table>.sql
# 3. Surgically restore into production (review first; wrap in a transaction):
psql "$PROD_URL" -1 -f /tmp/<table>.sql
```

## Full Postgres PITR (data-loss recovery)

```bash
titan flags set intake.read_only on          # stop writers first (durable: ledger still reads)
ops/scripts/restore.sh pitr --target-time "<RFC3339>" --into titan   # base + WAL replay to target
ops/scripts/reconcile-ledger.sh                # validate task-ledger consistency post-restore
titan flags set intake.read_only off         # reopen writes
```

## Qdrant restore

```bash
ops/scripts/restore.sh qdrant --snapshot <name>   # restore the latest 6h snapshot
# Degraded-but-operational sooner: agents run with reduced recall while re-embedding
# from Postgres source data (Vol 5) completes — that re-embed is the recovery of record.
```

## Redis rebuild (no data is lost — the ledger is the boundary)

```bash
# Start Redis empty, then rebuild the run queue from non-terminal ledger rows:
cd apps/api && uv run --env-file ../../.env python ../../ops/scripts/requeue-from-ledger.py
```

## Full-DR sequence (cluster loss — RTO ≤ 4 h, §8.3)

1. Provision cluster from `infra/` (Terraform + kustomize) — ~60 min.
2. Restore secrets from KMS — ~10 min.
3. Postgres PITR to latest WAL — ~30 min.
4. Start Redis empty → `requeue-from-ledger.py` — ~10 min.
5. Restore Qdrant snapshots (re-embed in background) — ~30 min.
6. Deploy last-known-good digests from the release manifest — ~20 min.
7. Run the smoke suite + ledger reconciliation — ~20 min.
8. Flip DNS — ~5 min.

## Verification

- Smoke suite green against the restored stack; `reconcile-ledger.sh` reports zero orphaned/duplicated tasks.
- Audit hash-chain verifies from the last seal (Phase 73) — no gap introduced by the restore.
- RPO/RTO measured against the table above; record the actuals.

## Escalation

- PITR can't reach the target time (missing WAL) → this is a **SEV-1 data-loss** incident (**[RUNBOOK-03](incident.md)**); recover to the newest reachable point and declare the gap.
- Object store itself is lost → invoke cross-bucket replica; if none, DR is limited by the oldest surviving base — declare scope honestly.

## Post-actions

- File a drill report in `ops/dr-drills/` (or an incident annex) with measured RTO/RPO.
- If a scheduled backup was missing, fix the schedule/credentials before clearing the page.

---

## Backup Failed

The **nightly backup-verify** job is red, or a scheduled backup is missing (OPS-018).
The platform is running on **stale or unverified** backups — a latent data-durability risk.

- **What it means:** either (a) the nightly restore-into-scratch failed its integrity check,
  or (b) `pushgateway`/the sidecar reported no fresh base/WAL within the window.
- **Immediate checks:**
  ```bash
  docker compose logs --tail=100 backup       # sidecar errors (creds? disk? object store down?)
  ops/scripts/restore.sh list                 # is there a fresh base (≤24h) and continuous WAL?
  ```
  - Object store unreachable / full? → the WAL archiver may also be stuck (see **[Postgres Lag](incident.md#postgres-lag)** — a stuck archiver fills the primary's disk).
  - Integrity check failed on restore? → the backups are **not trustworthy**; treat as a durability incident.
- **Mitigate:** restore the object-store/credential path first, then trigger an out-of-band
  `ops/scripts/backup.sh` and re-run the verify. Confirm WAL archiving resumed.
- **Escalate:** SEV-2. If both the archiver is stuck **and** the primary disk is filling,
  escalate to **SEV-1** (imminent primary outage) and follow **[RUNBOOK-03](incident.md)**.
- **Do not** silence this page by disabling the verify job — a missing backup is the failure, not the alert.
