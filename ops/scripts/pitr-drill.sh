#!/usr/bin/env bash
# `make pitr-drill` engine (Vol 9 §8.3 DoD, TITAN-OPS-018). Drives the full PITR round
# trip in ONE command with no manual steps:
#   (1) take a fresh backup → (2) record a target time → (3) destroy the database →
#   (4) PITR-restore to the target → (5) validate task-ledger consistency →
#   (6) run the smoke suite.
# A non-zero exit at any step fails the drill (fail closed).
#
# This REQUIRES Postgres + WAL archiving + an object store (MinIO/S3) and DESTROYS the
# target database — it runs in CI/staging against a disposable stack, NEVER on a dev
# laptop or any real database. It refuses to run unless TITAN_PITR_DRILL_CONFIRM=yes.
set -euo pipefail
cd "$(dirname "$0")"
source ./_backup_lib.sh

[ "${TITAN_PITR_DRILL_CONFIRM:-}" = "yes" ] \
  || die "refusing to run: this DESTROYS the target DB. Set TITAN_PITR_DRILL_CONFIRM=yes (CI/staging only)."
require_env BACKUP_S3_BUCKET BACKUP_AGE_RECIPIENT BACKUP_AGE_IDENTITY DATABASE_URL

DRILL_DB="${DRILL_DB:-titan_pitr_drill}"

log "1/6 taking a fresh backup (basebackup + WAL offload)"
./backup.sh basebackup
./backup.sh wal

# Give WAL a moment of real activity so the target time sits inside the recoverable window.
target="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "2/6 recorded PITR target time = ${target}"
psql "$DATABASE_URL" -c "CHECKPOINT;" >/dev/null 2>&1 || true
./backup.sh wal

log "3/6 destroying the drill database ${DRILL_DB} (simulated loss)"
psql "$DATABASE_URL" -c "DROP DATABASE IF EXISTS ${DRILL_DB};" >/dev/null

log "4/6 PITR-restoring to ${target} (boots the restored cluster + asserts integrity)"
TARGET_TIME="$target" INTO_DB="$DRILL_DB" ./restore.sh pitr --target-time "$target" --into "$DRILL_DB"
restored_dsn="$(cat "${PITR_DSN_FILE:-/tmp/titan-pitr.dsn}")"
restored_datadir="$(cat "${PITR_DATADIR_FILE:-/tmp/titan-pitr.datadir}")"
[ -n "$restored_dsn" ] && [ -n "$restored_datadir" ] || die "restore did not produce a running cluster"
_stop_restored() { "${PG_BIN:+$PG_BIN/}pg_ctl" -D "$restored_datadir" -m fast stop >&2 || true; }

# Validate the RESTORED cluster — NOT the live DB (a reconcile against the original would
# pass trivially and prove nothing about the restore).
log "5/6 validating task-ledger consistency against the RESTORED cluster ${restored_dsn}"
DATABASE_URL="$restored_dsn" ./reconcile-ledger.sh || { _stop_restored; die "restored ledger inconsistent"; }

log "6/6 running the smoke suite against the restored data"
if [ "${RUN_SMOKE:-1}" = "1" ]; then
  ( cd ../../apps/api && TITAN_DATABASE_URL="$restored_dsn" uv run pytest -m smoke --no-cov ) \
    || { _stop_restored; die "smoke suite failed on restored data"; }
fi

_stop_restored
log "PITR drill PASSED end-to-end (target=${target}, restored=${restored_dsn})"
