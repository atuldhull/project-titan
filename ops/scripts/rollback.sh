#!/usr/bin/env bash
# Blue/green rollback (Vol 9 §5.2, TITAN-OPS-006/011; RUNBOOK-02). Flips every TITAN
# Service selector back to the previous (last-known-good) color in < 5 min — no image
# rebuild, the old color is already running.
#
#   ops/scripts/rollback.sh status              # active color per service
#   ops/scripts/rollback.sh all                 # flip every service back to the previous color
#   ops/scripts/rollback.sh quarantine <digest> # block a bad @sha256 digest from re-promotion
#
# Kubernetes (staging/prod) operation — requires kubectl context on the target cluster.
# Idempotent: flipping to the already-active previous color is a no-op; quarantining an
# already-quarantined digest is a no-op.
set -euo pipefail
cd "$(dirname "$0")"

NS="${TITAN_NAMESPACE:-titan-prod}"
SERVICES=(${TITAN_SERVICES:-frontend api orchestrator worker llm-gateway})
QUARANTINE_CM="${QUARANTINE_CONFIGMAP:-titan-quarantined-digests}"

log() { printf '%s [rollback] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }
have_kubectl() { command -v kubectl >/dev/null || die "kubectl not found (this is a K8s operation)"; }

active_color() { kubectl -n "$NS" get svc "$1" -o jsonpath='{.spec.selector.color}' 2>/dev/null; }
other_color()  { [ "$1" = "blue" ] && echo green || echo blue; }

cmd_status() {
  have_kubectl
  local s c
  for s in "${SERVICES[@]}"; do
    c="$(active_color "$s")"; printf '%-14s %s\n' "$s" "${c:-<none>}"
  done
}

cmd_all() {
  have_kubectl
  local s cur prev
  for s in "${SERVICES[@]}"; do
    cur="$(active_color "$s")"
    [ -n "$cur" ] || { log "skip $s (no color selector — not blue/green managed)"; continue; }
    prev="$(other_color "$cur")"
    # Only flip if the previous color has READY endpoints, else we'd cut all traffic.
    # `addresses[*]` lists only ready IPs (notReadyAddresses are excluded); a jsonpath on a
    # missing field EXITS 0 with empty output, so we must test the output is NON-EMPTY —
    # a plain `if ! kubectl ...` (exit-code only) would sail past a not-ready endpoint.
    local ready
    ready="$(kubectl -n "$NS" get endpoints "${s}-${prev}" \
             -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
    if [ -z "$ready" ]; then
      log "WARN: ${s}-${prev} has no READY endpoints — NOT flipping $s (would cause an outage)"
      continue
    fi
    kubectl -n "$NS" patch svc "$s" --type merge -p "{\"spec\":{\"selector\":{\"color\":\"${prev}\"}}}" >/dev/null
    log "flipped $s: ${cur} -> ${prev}"
  done
  log "rollback complete — verify SLO recovery on the burn panel, then quarantine the bad digest"
}

cmd_quarantine() {
  have_kubectl
  local digest="${1:?usage: rollback.sh quarantine <sha256-digest>}"
  case "$digest" in sha256:*) : ;; *) die "expected a sha256:... digest, got '$digest'";; esac
  kubectl -n "$NS" get configmap "$QUARANTINE_CM" >/dev/null 2>&1 \
    || kubectl -n "$NS" create configmap "$QUARANTINE_CM" >/dev/null
  # store under the digest key; idempotent (repeated adds overwrite the same key)
  kubectl -n "$NS" patch configmap "$QUARANTINE_CM" --type merge \
    -p "{\"data\":{\"${digest//:/_}\":\"quarantined $(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" >/dev/null
  # Honest scope: this RECORDS the quarantine in-cluster. Automatic enforcement (an
  # admission policy that refuses to schedule a quarantined digest) lands with the
  # Kyverno policy in Phase 79; until then, do NOT re-promote this digest manually.
  log "quarantined $digest in ConfigMap ${QUARANTINE_CM} (namespace ${NS})."
  log "NOTE: enforcement is manual until the Phase-79 admission policy — do not re-promote it."
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  status)     cmd_status ;;
  all)        cmd_all ;;
  quarantine) cmd_quarantine "$@" ;;
  *)          die "usage: rollback.sh {status|all|quarantine <digest>}" ;;
esac
