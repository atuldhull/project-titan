#!/usr/bin/env bash
# TITAN restore operations (Vol 9 §8.3, TITAN-OPS-018/019). Companion to backup.sh.
#
#   ops/scripts/restore.sh list                                  # inventory + freshness
#   ops/scripts/restore.sh pitr --target-time <RFC3339> --into <db>   # base + WAL replay
#   ops/scripts/restore.sh qdrant --snapshot <name>              # restore a collection snapshot
#   ops/scripts/restore.sh verify                                # nightly restore-into-scratch check
#
# `pitr` and `qdrant` mutate real infra and are STAGING/PROD/CI operations — never run
# on a laptop. `verify` is what nightly.yml calls: restore the latest base+WAL into a
# throwaway scratch database and run integrity checks; a non-zero exit fires BackupFailed.
set -euo pipefail
cd "$(dirname "$0")"
source ./_backup_lib.sh

PGHOST="${PGHOST:-postgres}"; PGUSER="${PGUSER:-titan}"
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"

usage() { die "usage: restore.sh {list|pitr --target-time <ts> --into <db>|qdrant --snapshot <n>|verify}"; }

restore_list() {
  require_env BACKUP_S3_BUCKET
  echo "== postgres/base ==";  _aws s3 ls "$(s3_base)/postgres/base/" | tail -5 || true
  echo "== postgres/wal (newest) =="; _aws s3 ls "$(s3_base)/postgres/wal/"  | tail -3 || true
  echo "== qdrant ==";        _aws s3 ls "$(s3_base)/qdrant/" --recursive | tail -5 || true
  echo "== redis ==";         _aws s3 ls "$(s3_base)/redis/" | tail -3 || true
  # Freshness gate: newest base must be within 24h, else this is effectively a failure.
  local newest age_h
  newest="$(_aws s3 ls "$(s3_base)/postgres/base/" | sort | tail -1 | awk '{print $1" "$2}')"
  [ -n "$newest" ] || die "no postgres base backup found"
  age_h=$(( ( $(date +%s) - $(date -d "$newest" +%s 2>/dev/null || echo 0) ) / 3600 ))
  log "newest base backup is ${age_h}h old"
  [ "$age_h" -le 24 ] || die "newest base backup is stale (${age_h}h > 24h)"
}

# Fetch+decrypt the latest base into $1, then stage WAL for replay to $TARGET_TIME.
_stage_base_and_wal() {
  local dest="$1" key
  key="$(_aws s3 ls "$(s3_base)/postgres/base/" | sort | tail -1 | awk '{print $4}')"
  [ -n "$key" ] || die "no base backup to restore"
  mkdir -p "$dest"
  _aws s3 cp "$(s3_base)/postgres/base/$key" - | decrypt | tar -xz -C "$dest"
  # recovery.signal + restore_command pull WAL back from the store on demand. The
  # restore_command runs under `bash -c set -o pipefail` so a failed WAL FETCH aborts the
  # command (non-zero) instead of handing Postgres a truncated/empty %p (silent gap).
  : > "$dest/recovery.signal"
  local ep="${BACKUP_S3_ENDPOINT:+--endpoint-url ${BACKUP_S3_ENDPOINT} }"
  cat > "$dest/postgresql.auto.conf" <<EOF
restore_command = 'bash -c "set -o pipefail; aws ${ep}s3 cp $(s3_base)/postgres/wal/%f.age - | age -d -i ${BACKUP_AGE_IDENTITY} > %p"'
recovery_target_time = '${TARGET_TIME}'
recovery_target_action = 'promote'
EOF
}

# Boot Postgres on a staged datadir (recovery replays WAL to the target + promotes),
# then ASSERT the cluster is queryable AND carries the system-of-record tables. Leaves
# the cluster running and echoes its DSN on stdout. Fails CLOSED: if the base won't open,
# the WAL chain won't replay, or core tables are absent, it `die`s — so verify/drill go
# red instead of blessing an unrestorable backup (the whole point of OPS-018).
_boot_pg() {
  local datadir="$1" port="${2:-5433}" db="${PGDATABASE:-titan}"
  local pgctl="${PG_BIN:+$PG_BIN/}pg_ctl" psql_bin="${PG_BIN:+$PG_BIN/}psql"
  chmod 700 "$datadir"
  "$pgctl" -D "$datadir" -o "-p $port -c listen_addresses=localhost" -w -t 180 start >&2
  local dsn="postgresql://${PGUSER}@localhost:${port}/${db}"
  "$psql_bin" "$dsn" -v ON_ERROR_STOP=1 -tAc "SELECT 1" >/dev/null
  local ok
  ok="$("$psql_bin" "$dsn" -v ON_ERROR_STOP=1 -tAc \
    "SELECT (to_regclass('public.tasks') IS NOT NULL AND to_regclass('public.audit_events') IS NOT NULL)")"
  if [ "$ok" != "t" ]; then
    "$pgctl" -D "$datadir" -m fast stop >&2 || true
    die "restored cluster is missing core tables (tasks/audit_events) — backup is not restorable"
  fi
  printf '%s' "$dsn"
}

_stop_pg() { "${PG_BIN:+$PG_BIN/}pg_ctl" -D "$1" -m fast stop >&2 || true; }

restore_pitr() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_IDENTITY TARGET_TIME
  [ -n "${INTO_DB:-}" ] || usage
  local datadir="${PITR_DATADIR:-$(mktemp -d)}"
  log "PITR: restoring base + replaying WAL to ${TARGET_TIME} into datadir ${datadir}"
  _stage_base_and_wal "$datadir"
  local dsn
  dsn="$(_boot_pg "$datadir" "${PITR_PORT:-5434}")"  # actually boots + replays + asserts
  log "PITR restore complete and RUNNING at ${dsn} (datadir ${datadir})"
  # hand the restored DSN + datadir to the caller (the drill) so it validates the
  # RESTORED cluster, not the live one, then stops it.
  printf '%s\n' "$dsn" > "${PITR_DSN_FILE:-/tmp/titan-pitr.dsn}"
  printf '%s\n' "$datadir" > "${PITR_DATADIR_FILE:-/tmp/titan-pitr.datadir}"
  log "validate with: DATABASE_URL='${dsn}' ops/scripts/reconcile-ledger.sh; stop with: pg_ctl -D ${datadir} stop"
}

restore_qdrant() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_IDENTITY
  [ -n "${SNAPSHOT:-}" ] || usage
  log "restoring Qdrant snapshot ${SNAPSHOT} (re-embedding from Postgres is the recovery of record)"
  _aws s3 cp "$(s3_base)/qdrant/${SNAPSHOT}" - | decrypt > /tmp/qdrant-snap
  curl -sf -X POST "$QDRANT_URL/collections/${QDRANT_COLLECTION:?}/snapshots/upload" \
    -H 'Content-Type: multipart/form-data' -F "snapshot=@/tmp/qdrant-snap" >/dev/null
  rm -f /tmp/qdrant-snap
}

# Nightly restore-verify (OPS-018): stage latest base+WAL, BOOT a throwaway cluster so
# recovery actually replays the WAL to the target, ASSERT integrity, then record success.
# This is not a log line — an unrestorable base or a broken WAL chain makes it exit non-zero
# (fail closed), which fires the BackupFailed page. record_success runs ONLY after the
# assertion passes, so the gauge can never claim success on a backup we couldn't open.
restore_verify() {
  require_env BACKUP_S3_BUCKET BACKUP_AGE_IDENTITY
  local datadir; datadir="$(mktemp -d)"
  : "${TARGET_TIME:=$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  log "restore-verify: staging latest base+WAL, then booting a scratch cluster to replay + assert"
  _stage_base_and_wal "$datadir"
  local dsn
  dsn="$(_boot_pg "$datadir" "${VERIFY_PORT:-5433}")"
  log "restored cluster queryable with core tables present: ${dsn}"
  _stop_pg "$datadir"
  rm -rf "$datadir"
  log "restore-verify OK (base + WAL replayed into a scratch cluster and integrity-checked)"
  record_success
}

cmd="${1:-}"; shift || true
# parse the long flags the subcommands share
while [ $# -gt 0 ]; do
  case "$1" in
    --target-time) TARGET_TIME="$2"; shift 2 ;;
    --into)        INTO_DB="$2"; shift 2 ;;
    --snapshot)    SNAPSHOT="$2"; shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done
export TARGET_TIME INTO_DB SNAPSHOT 2>/dev/null || true

case "$cmd" in
  list)   restore_list ;;
  pitr)   restore_pitr ;;
  qdrant) restore_qdrant ;;
  verify) restore_verify ;;
  *)      usage ;;
esac
