#!/usr/bin/env bash
# One-shot TITAN backup (Vol 9 §8.2, TITAN-OPS-018). Encrypted artifacts to object store.
#
#   ops/scripts/backup.sh wal          # offload archived WAL segments (continuous)
#   ops/scripts/backup.sh basebackup   # daily pg_basebackup (base of the PITR window)
#   ops/scripts/backup.sh qdrant       # 6-hourly Qdrant collection snapshots
#   ops/scripts/backup.sh redis        # hourly Redis RDB
#   ops/scripts/backup.sh all          # basebackup + qdrant + redis + wal (used by drills)
#
# Idempotent: re-running any subcommand is safe (WAL offload skips already-shipped
# segments; snapshots/RDB are timestamped). Runs in the Compose `backup` sidecar or a
# CI/staging runner — NOT verifiable on the local dev laptop (no MinIO). Fails CLOSED:
# a missing tool or credential aborts before it can report a false success.
set -euo pipefail
cd "$(dirname "$0")"
source ./_backup_lib.sh

# Postgres connection + local paths, all env-driven (Compose/K8s inject them).
PGHOST="${PGHOST:-postgres}"; PGUSER="${PGUSER:-titan}"; PGDATABASE="${PGDATABASE:-titan}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/wal-archive}"     # where postgres archive_command drops WAL
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
REDIS_HOST="${REDIS_HOST:-redis}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

backup_wal() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT
  [ -d "$WAL_ARCHIVE_DIR" ] || die "WAL archive dir $WAL_ARCHIVE_DIR not mounted"
  local f base
  shopt -s nullglob
  for f in "$WAL_ARCHIVE_DIR"/*; do
    base="$(basename "$f")"
    # Ship unless already in the store (idempotent, continuous), then delete the local
    # copy: /wal-archive is a STAGING area, so pruning only S3 would fill the disk.
    if ! _aws s3 ls "$(s3_base)/postgres/wal/${base}.age" >/dev/null 2>&1; then
      put_encrypted "$f" "postgres/wal/${base}.age"
    fi
    rm -f "$f"
  done
  prune_older_than "postgres/wal" "${WAL_RETENTION_DAYS:-7}"
  record_success
}

backup_basebackup() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT
  local dir; dir="$(mktemp -d)"
  # -Ft -z → a single compressed tar stream we can encrypt and ship atomically.
  pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$dir" -Ft -z -Xs -P
  put_encrypted "$dir/base.tar.gz" "postgres/base/base-${STAMP}.tar.gz.age"
  rm -rf "$dir"
  prune_older_than "postgres/base" "${BASE_RETENTION_DAYS:-30}"
  record_success
}

backup_qdrant() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT
  local col snap tmp cols
  tmp="$(mktemp -d)"
  # Capture the collection list FIRST and fail closed if the listing errors — a bare
  # `for col in $(curl ... )` would run zero times on a Qdrant outage and fall through to
  # record_success, silently reporting a backup that shipped nothing (a fail-open). An
  # EMPTY list (a healthy Qdrant with no collections) is legitimately a no-op.
  cols="$(curl -sf "$QDRANT_URL/collections" | python3 -c \
      'import sys,json;print("\n".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))')" \
    || die "qdrant collection listing failed — refusing to record a backup success"
  for col in $cols; do
    snap="$(curl -sf -X POST "$QDRANT_URL/collections/$col/snapshots" \
            | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["name"])')"
    curl -sf "$QDRANT_URL/collections/$col/snapshots/$snap" -o "$tmp/$col-$snap"
    put_encrypted "$tmp/$col-$snap" "qdrant/${col}/${STAMP}-${snap}.age"
  done
  rm -rf "$tmp"
  prune_older_than "qdrant" "${QDRANT_RETENTION_DAYS:-14}"
  record_success
}

backup_redis() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT
  local tmp; tmp="$(mktemp -d)"
  # Trigger a fresh RDB and copy it out. Redis SAVE is synchronous; BGSAVE + wait is nicer
  # but SAVE keeps the script simple and the dataset small (queue/cache only).
  redis-cli -h "$REDIS_HOST" --rdb "$tmp/dump.rdb" >/dev/null
  put_encrypted "$tmp/dump.rdb" "redis/dump-${STAMP}.rdb.age"
  rm -rf "$tmp"
  prune_older_than "redis" "${REDIS_RETENTION_DAYS:-2}"   # 48h
  record_success
}

cmd="${1:-all}"
require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT
ensure_bucket
case "$cmd" in
  wal)        backup_wal ;;
  basebackup) backup_basebackup ;;
  qdrant)     backup_qdrant ;;
  redis)      backup_redis ;;
  all)        backup_basebackup; backup_qdrant; backup_redis; backup_wal ;;
  *)          die "usage: backup.sh {wal|basebackup|qdrant|redis|all}" ;;
esac
log "backup '$cmd' complete"
