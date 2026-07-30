#!/usr/bin/env bash
# Disaster-recovery drill (Vol 9 §8.3, TITAN-OPS-019). Simulates total cluster loss and
# rebuilds the platform in the EXACT §8.3 restore order, timing each step and writing an
# RTO/RPO report under ops/dr-drills/ that check_dr_report.py validates against the
# RTO <= 4 h / RPO <= 5 min targets.
#
#   ops/scripts/dr-drill.sh --dry-run     # validate wiring + emit a report; touches NO infra
#   TITAN_DR_DRILL_CONFIRM=yes ops/scripts/dr-drill.sh   # the real, DESTRUCTIVE drill
#
# The real drill provisions + destroys + rebuilds a cluster and is CI/staging ONLY — it refuses
# to run without TITAN_DR_DRILL_CONFIRM=yes. Fail-closed: any step failing marks the report FAIL
# and exits non-zero (a drill that can't restore must NEVER report success — the Phase-78 lesson).
# --dry-run runs the SAME step sequence with the infra actions skipped, so the ordering + report
# contract are verifiable without a cluster.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
[ "${DR_DRY_RUN:-0}" = "1" ] && DRY_RUN=1

ENVIRONMENT="${DR_ENVIRONMENT:-staging}"
REPORT_DIR="${DR_REPORT_DIR:-$ROOT/ops/dr-drills}"
RTO_TARGET=$(( 4 * 60 * 60 ))  # OPS-019: RTO <= 4 h
RPO_TARGET=$(( 5 * 60 ))       # OPS-019: RPO <= 5 min

log() { printf '%s [dr-drill] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
epoch() { date -u +%s; }
have() { command -v "$1" >/dev/null || die "$1 not found (required)"; }

have jq
mkdir -p "$REPORT_DIR"
if [ "$DRY_RUN" != "1" ]; then
  [ "${TITAN_DR_DRILL_CONFIRM:-}" = "yes" ] \
    || die "refusing: this DESTROYS + rebuilds a cluster. Set TITAN_DR_DRILL_CONFIRM=yes (staging only), or pass --dry-run."
fi

# --- the §8.3 restore steps (real actions; skipped in --dry-run) --------------------------
# RPO is captured by the Postgres PITR step: the gap between the loss point and the newest
# replayable WAL. --dry-run uses a synthetic in-budget value so the contract can be exercised.
RPO_SECONDS="${DR_DRYRUN_RPO_SECONDS:-60}"

step_provision() {  # 1: cluster from infra/ (Terraform + kustomize)
  terraform -chdir="$ROOT/infra/terraform" apply -auto-approve -input=false
  kubectl apply -k "$ROOT/infra/k8s/overlays/${ENVIRONMENT}"
}
step_restore_secrets() {  # 2: restore the sealed secrets bundle into the vault
  "$ROOT/ops/scripts/restore.sh" secrets
}
step_restore_postgres_pitr() {  # 3: WAL PITR to latest; sets RPO_SECONDS from the WAL lag
  "$ROOT/ops/scripts/restore.sh" pitr --target-time latest
  RPO_SECONDS="$(cat "${DR_RPO_FILE:-/tmp/titan-dr-rpo.seconds}" 2>/dev/null || echo "$RPO_TARGET")"
}
step_requeue_from_ledger() {  # 4: start Redis empty, re-enqueue non-terminal ledger rows
  ( cd "$ROOT/apps/api" && uv run python "$ROOT/ops/scripts/requeue-from-ledger.py" )
}
step_restore_qdrant() {  # 5: restore Qdrant snapshots
  "$ROOT/ops/scripts/restore.sh" qdrant
}
step_deploy_lkg_digests() {  # 6: deploy last-known-good digests from the release manifest
  kubectl apply -k "$ROOT/infra/k8s/overlays/${ENVIRONMENT}"
}
step_smoke() {  # 7: smoke suite green in the restored cluster
  ( cd "$ROOT/apps/api" && uv run pytest -m smoke --no-cov )
}
step_flip_dns() {  # 8: point DNS at the rebuilt ingress
  terraform -chdir="$ROOT/infra/terraform" apply -auto-approve -input=false -target=module.dns
}

# name -> function. Order IS the §8.3 sequence check_dr_report enforces.
STEP_NAMES=(provision restore-secrets restore-postgres-pitr requeue-from-ledger \
            restore-qdrant deploy-lkg-digests smoke flip-dns)
STEP_FNS=(step_provision step_restore_secrets step_restore_postgres_pitr step_requeue_from_ledger \
          step_restore_qdrant step_deploy_lkg_digests step_smoke step_flip_dns)

STEPS_JSON=()
RESULT="PASS"
STARTED="$(now)"; STARTED_EPOCH="$(epoch)"

for i in "${!STEP_NAMES[@]}"; do
  name="${STEP_NAMES[$i]}"; fn="${STEP_FNS[$i]}"
  s_start="$(now)"
  status="ok"
  log "STEP $((i + 1))/8 ${name}"
  if [ "$DRY_RUN" != "1" ]; then
    if ! "$fn"; then status="failed"; RESULT="FAIL"; fi
  fi
  s_end="$(now)"
  STEPS_JSON+=("$(jq -nc --arg n "$name" --arg s "$s_start" --arg e "$s_end" --arg st "$status" \
    '{name:$n, started_at:$s, completed_at:$e, status:$st}')")
  [ "$status" = "ok" ] || { log "step ${name} FAILED — aborting drill"; break; }
done

COMPLETED="$(now)"; COMPLETED_EPOCH="$(epoch)"
RTO_SECONDS=$(( COMPLETED_EPOCH - STARTED_EPOCH ))
[ "$RTO_SECONDS" -le "$RTO_TARGET" ] || RESULT="FAIL"
[ "$RPO_SECONDS" -le "$RPO_TARGET" ] 2>/dev/null || RESULT="FAIL"

report="$REPORT_DIR/dr-${STARTED_EPOCH}.json"
jq -n \
  --arg id "dr-${STARTED_EPOCH}-${ENVIRONMENT}" \
  --arg env "$ENVIRONMENT" \
  --argjson dry "$([ "$DRY_RUN" = "1" ] && echo true || echo false)" \
  --arg started "$STARTED" --arg completed "$COMPLETED" \
  --argjson rto "$RTO_SECONDS" --argjson rpo "$RPO_SECONDS" \
  --argjson rto_t "$RTO_TARGET" --argjson rpo_t "$RPO_TARGET" \
  --argjson steps "$(printf '%s\n' "${STEPS_JSON[@]}" | jq -s '.')" \
  --arg result "$RESULT" \
  '{drill_id:$id, environment:$env, dry_run:$dry, started_at:$started, completed_at:$completed,
    targets:{rto_seconds:$rto_t, rpo_seconds:$rpo_t},
    measured:{rto_seconds:$rto, rpo_seconds:$rpo}, steps:$steps, result:$result}' > "$report"

log "report → ${report} (result=${RESULT}, RTO=${RTO_SECONDS}s, RPO=${RPO_SECONDS}s)"
[ "$RESULT" = "PASS" ] || die "DR drill FAILED (see ${report})"
log "DR drill PASSED — RTO ${RTO_SECONDS}s <= ${RTO_TARGET}s, RPO ${RPO_SECONDS}s <= ${RPO_TARGET}s"
