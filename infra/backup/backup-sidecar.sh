#!/usr/bin/env bash
# TITAN backup sidecar loop (Vol 9 §8.2 schedule, TITAN-OPS-018). Continuous WAL offload
# plus scheduled base / Qdrant / Redis backups, each encrypted and shipped to the object
# store by ops/scripts/backup.sh. Runs forever in the Compose `backup` service (ON by
# default so self-hosters get backups without opting in — risk R5). All timing/retention
# is env-driven; the heavy lifting and the fail-closed record_success live in backup.sh.
set -euo pipefail

SCRIPTS="${TITAN_SCRIPTS_DIR:-/opt/titan/ops/scripts}"
LOOP_SECONDS="${BACKUP_LOOP_SECONDS:-60}"      # WAL offload cadence
BASE_HOUR="${BACKUP_BASE_HOUR:-02}"            # daily basebackup at 02:00 UTC
QDRANT_EVERY_H="${BACKUP_QDRANT_EVERY_HOURS:-6}"
REDIS_EVERY_H="${BACKUP_REDIS_EVERY_HOURS:-1}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/wal-archive}"
WAL_WARN_MB="${BACKUP_WAL_WARN_MB:-2048}"      # warn if the staging dir grows past this

log() { printf '%s [sidecar] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
run() { "$SCRIPTS/backup.sh" "$1" || log "WARN: backup.sh $1 failed this cycle (will retry)"; }

# If WAL can't ship (object store down), backup.sh keeps the un-shipped segments on the
# staging volume rather than lose them — but that volume can then grow. Surface it loudly
# so a persistent offload failure is visible before Postgres' pg_wal ever backs up.
wal_dir_guard() {
  local mb
  mb="$(du -sm "$WAL_ARCHIVE_DIR" 2>/dev/null | awk '{print $1}')"
  [ -n "$mb" ] || return 0
  if [ "$mb" -gt "$WAL_WARN_MB" ]; then
    log "WARN: WAL staging dir ${WAL_ARCHIVE_DIR} is ${mb}MB (> ${WAL_WARN_MB}MB) — object store offload may be failing; BackupFailed will fire if no success in 25h"
  fi
}

last_base=""; last_qdrant_epoch=0; last_redis_epoch=0
log "starting: WAL every ${LOOP_SECONDS}s, base @${BASE_HOUR}:00 UTC, qdrant/${QDRANT_EVERY_H}h, redis/${REDIS_EVERY_H}h"

while true; do
  now_epoch="$(date +%s)"; today="$(date -u +%Y%m%d)"; hour="$(date -u +%H)"

  run wal                                                    # continuous WAL offload
  wal_dir_guard                                              # loud warning if offload is stalling

  if [ "$hour" = "$BASE_HOUR" ] && [ "$today" != "$last_base" ]; then
    run basebackup && last_base="$today"
  fi
  if [ $(( now_epoch - last_qdrant_epoch )) -ge $(( QDRANT_EVERY_H * 3600 )) ]; then
    run qdrant && last_qdrant_epoch="$now_epoch"
  fi
  if [ $(( now_epoch - last_redis_epoch )) -ge $(( REDIS_EVERY_H * 3600 )) ]; then
    run redis && last_redis_epoch="$now_epoch"
  fi

  sleep "$LOOP_SECONDS"
done
