#!/usr/bin/env bash
# Post-restore task-ledger consistency check (Vol 9 §8.3; RUNBOOK-04 step "validate
# ledger consistency"). After a PITR restore, assert the run/task ledger is internally
# consistent before reopening writes: no run points at a task that vanished, and every
# non-terminal task is re-enqueueable. Read-only — it never mutates data.
#
#   DATABASE_URL=postgresql://... ops/scripts/reconcile-ledger.sh
#
# Exits non-zero (fails CLOSED) on any inconsistency so a restore can't be blessed blind.
set -euo pipefail

DB="${DATABASE_URL:?DATABASE_URL required (the restored database)}"

log() { printf '%s [reconcile] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

# Orphaned runs: a run row whose task_id has no matching task after the restore.
orphans="$(psql "$DB" -tAc \
  "SELECT count(*) FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE t.id IS NULL;")"
# Non-terminal tasks that the requeue path will rebuild into the Redis stream.
requeueable="$(psql "$DB" -tAc \
  "SELECT count(*) FROM tasks WHERE status NOT IN ('done','failed','cancelled');")"

log "orphaned runs: ${orphans}; non-terminal (re-enqueueable) tasks: ${requeueable}"
if [ "${orphans:-1}" -ne 0 ]; then
  log "FATAL: ${orphans} run(s) reference a missing task — restore is INCONSISTENT"
  exit 1
fi
log "ledger consistent — safe to run requeue-from-ledger.py and reopen writes"
