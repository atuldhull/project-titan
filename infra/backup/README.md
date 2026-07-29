# TITAN backups (Vol 9 §8.2/§8.3, TITAN-OPS-018)

Encrypted backups of the three stateful stores, shipped to S3-compatible object storage.
The Compose `backup` sidecar + `minio` live in the **`backup` profile**, which
`.env.example` turns **ON by default** (`COMPOSE_PROFILES=backup`) so the documented
`cp .env.example .env && docker compose up` gets backups without opting in (risk R5). Run
a lean stack with `COMPOSE_PROFILES= docker compose up`.

The sidecar **fails closed**: with `BACKUP_AGE_RECIPIENT` unset it refuses to ship (it will
never store plaintext or record a fake success), so complete the one-time bootstrap below
before relying on it. The sidecar only *encrypts*, so it holds no private key; **restore**
supplies the age identity out-of-band (never a committed compose secret).

## What runs

| Component | Role |
|---|---|
| `postgres` (WAL archiving) | `archive_mode=on`, `archive_timeout=60` → each segment lands in the `walarchive` staging volume (RPO ≤ 5 min) |
| `minio` | local S3-compatible object store (`titan-backups` bucket) |
| `backup` sidecar | `infra/backup/backup-sidecar.sh` loop: continuous WAL offload + daily basebackup + 6-hourly Qdrant snapshots + hourly Redis RDB, each **age-encrypted** via `ops/scripts/backup.sh` |

Retention (§8.2): base 30 d, WAL 7 d (the PITR window), Qdrant 14 d, Redis 48 h — enforced
by `prune_older_than` after each ship.

## One-time local bootstrap

```bash
cp .env.example .env                                    # COMPOSE_PROFILES=backup is preset
cd apps/api && uv run python -m titan.vault keygen      # prints AGE-SECRET-KEY-1… + age1… recipient
printf 'AGE-SECRET-KEY-1…\n' > infra/backup/local-age.key   # the PRIVATE key — gitignored, never commit (restore only)
# put the age1… recipient into BACKUP_AGE_RECIPIENT in .env, then:
docker compose up -d minio backup                       # the sidecar auto-creates the titan-backups bucket
```

The sidecar **fails closed**: with `BACKUP_AGE_RECIPIENT` unset it refuses to ship (a backup
that encrypts to nothing is worse than none) — it logs and retries rather than storing
plaintext. `record_success` (which clears the `BackupFailed` page) only fires *after* an
artifact is confirmed in the store, so a half-finished or WAL-less run can't fake a green.
The bucket is created idempotently on first use, so no manual `mc mb` step is needed.

## Restore & drills

- `ops/scripts/restore.sh list` — inventory + freshness gate (newest base ≤ 24 h).
- `ops/scripts/restore.sh verify` — nightly restore-into-scratch + integrity check (CI: `nightly.yml`).
- `ops/scripts/restore.sh pitr --target-time <RFC3339> --into <db>` — point-in-time restore.
- `make pitr-drill` — full round trip (backup → destroy → PITR → reconcile → smoke), CI/staging only.

See [RUNBOOK-04](../../ops/runbooks/backup-restore.md) for the operator procedures and RPO/RTO targets.

## Production notes (not this repo)

- The age **private** key comes from the KMS-backed secret store (Vol 8), mounted as the
  `backup-age-key` secret — never a repo file.
- Object storage is the managed store (S3/GCS) with server-side encryption + cross-bucket
  replication; MinIO here is the local-parity stand-in.
- Base images are digest-pinned per OPS-005; the floating tags here match the local stack.
