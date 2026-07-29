#!/usr/bin/env bash
# Shared helpers for TITAN backup/restore scripting (Vol 9 §8.2/§8.3, TITAN-OPS-018).
#
# Encryption (AES-256 equivalent via age/X25519, keys sourced from KMS in prod — Vol 8)
# and object-store transfer (S3-compatible; MinIO acceptable) are centralized here so
# backup.sh / restore.sh / pitr-drill.sh stay small and consistent. Source, don't run:
#   source "$(dirname "$0")/_backup_lib.sh"
#
# This library is designed to run inside the Compose `backup` sidecar or any CI/staging
# runner that has: bash, awscli or mc, age, pg_basebackup/pg_dump, curl. It is NOT
# exercised on the local dev laptop (no MinIO/PITR there) — CI/staging is its home.

set -euo pipefail

log()  { printf '%s [backup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die()  { log "FATAL: $*"; exit 1; }

# Fail closed on missing configuration rather than silently backing up nothing.
require_env() {
  local missing=0 v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then log "missing required env: $v"; missing=1; fi
  done
  [ "$missing" -eq 0 ] || die "refusing to run with incomplete configuration"
}

# Object-store prefix, e.g. s3://titan-backups/postgres . BACKUP_S3_BUCKET is required.
s3_base() { printf 's3://%s' "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET unset}"; }

# Idempotently ensure the backup bucket exists, so a fresh MinIO doesn't turn every
# backup into a silent NoSuchBucket no-op (the sidecar would look "up" while shipping
# nothing). Cheap: one ls, create only if absent.
ensure_bucket() {
  _aws s3 ls "$(s3_base)" >/dev/null 2>&1 || _aws s3 mb "$(s3_base)"
}

# aws s3 wrapper honoring a custom endpoint (MinIO). AWS_* / BACKUP_S3_ENDPOINT from env.
_aws() {
  local endpoint_args=()
  [ -n "${BACKUP_S3_ENDPOINT:-}" ] && endpoint_args=(--endpoint-url "$BACKUP_S3_ENDPOINT")
  aws "${endpoint_args[@]}" "$@"
}

# Encrypt stdin → stdout with age. BACKUP_AGE_RECIPIENT is the public key (prod: from KMS).
encrypt() { age -r "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT unset}"; }
# Decrypt stdin → stdout. BACKUP_AGE_IDENTITY is the private-key file (prod: mounted from KMS).
decrypt() { age -d -i "${BACKUP_AGE_IDENTITY:?BACKUP_AGE_IDENTITY unset}"; }

# Stream a local file to the object store, encrypted, atomically (temp key then copy).
put_encrypted() {
  local src="$1" key="$2"
  encrypt < "$src" | _aws s3 cp - "$(s3_base)/$key" >/dev/null
  log "stored $(s3_base)/$key ($(wc -c < "$src") bytes plaintext)"
}

# Record success for the BackupFailed alert. Prefer a Pushgateway; fall back to a
# node_exporter textfile. NEVER report success on a failed backup (fail closed): callers
# invoke this ONLY after the artifact is confirmed in the object store.
record_success() {
  local metric="titan_backup_last_success_timestamp_seconds"
  local now; now="$(date +%s)"
  if [ -n "${PUSHGATEWAY_URL:-}" ]; then
    printf '# TYPE %s gauge\n%s %s\n' "$metric" "$metric" "$now" \
      | curl -sf --data-binary @- "${PUSHGATEWAY_URL}/metrics/job/titan_backup" \
      || log "WARN: pushgateway update failed (backup itself succeeded)"
  fi
  if [ -n "${BACKUP_TEXTFILE_DIR:-}" ]; then
    printf '# TYPE %s gauge\n%s %s\n' "$metric" "$metric" "$now" \
      > "${BACKUP_TEXTFILE_DIR}/titan_backup.prom.tmp"
    mv "${BACKUP_TEXTFILE_DIR}/titan_backup.prom.tmp" "${BACKUP_TEXTFILE_DIR}/titan_backup.prom"
  fi
  log "recorded backup success at epoch $now"
}

# Prune object-store keys under a prefix older than N days (retention enforcement).
prune_older_than() {
  local prefix="$1" days="$2" cutoff line key ts
  cutoff="$(date -u -d "@$(( $(date +%s) - days*86400 ))" +%Y-%m-%d 2>/dev/null \
           || date -u -r "$(( $(date +%s) - days*86400 ))" +%Y-%m-%d)"
  _aws s3 ls "$(s3_base)/$prefix/" | while read -r line; do
    ts="$(printf '%s' "$line" | awk '{print $1}')"
    key="$(printf '%s' "$line" | awk '{print $4}')"
    [ -n "$key" ] || continue
    if [[ "$ts" < "$cutoff" ]]; then
      _aws s3 rm "$(s3_base)/$prefix/$key" >/dev/null && log "pruned $prefix/$key (>${days}d)"
    fi
  done
}
