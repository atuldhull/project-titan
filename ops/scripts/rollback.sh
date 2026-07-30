#!/usr/bin/env bash
# Blue/green rollback (Vol 9 §5.2, TITAN-OPS-006/011; RUNBOOK-02). Flips TITAN Service selectors
# to the previous (last-known-good) color in < 5 min — no image rebuild, the old color is warm
# during the 30-min post-flip window.
#
#   ops/scripts/rollback.sh status                 # active color per service
#   ops/scripts/rollback.sh all [<color>]          # flip every service to <color> (default: toggle)
#   ops/scripts/rollback.sh <service> [<color>]    # flip one service to <color> (default: toggle)
#   ops/scripts/rollback.sh quarantine <digest>    # block a bad @sha256 digest from re-promotion
#
# Give an EXPLICIT <color> for automated/known-good rollback (release.sh passes the last-good
# color): a bare toggle re-promotes the bad color if run twice or against a mixed state, so the
# explicit form is the safe one. Every flip is guarded by READY endpoints on the target color
# (never cut traffic to a color that isn't up). Resilient: one service's error does not abort the
# rest — it attempts ALL services and returns non-zero if ANY failed, so a half-rollback can't be
# silently reported as complete.
set -euo pipefail

NS="${TITAN_NAMESPACE:-titan-prod}"
SERVICES=(${TITAN_SERVICES:-frontend api orchestrator worker llm-gateway})
QUARANTINE_CM="${QUARANTINE_CONFIGMAP:-titan-quarantined-digests}"

log() { printf '%s [rollback] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }
have_kubectl() { command -v kubectl >/dev/null || die "kubectl not found (this is a K8s operation)"; }
other_color() { [ "$1" = "blue" ] && echo green || echo blue; }

# Flip ONE service to a target color. $2 empty ⇒ toggle to the other color. Guards: read error
# ⇒ return 1 (counted, loop continues); no color selector (worker) ⇒ safe skip; already on
# target ⇒ no-op; target has no READY endpoints ⇒ refuse (would cut traffic). Never `exit`.
flip_one() {
  local s="$1" want="${2:-}" cur target ready
  if ! cur="$(kubectl -n "$NS" get svc "$s" -o jsonpath='{.spec.selector.color}' 2>/dev/null)"; then
    log "ERROR: cannot read color of svc $s (skipped, NOT flipped)"; return 1
  fi
  [ -n "$cur" ] || { log "skip $s (no color selector — not blue/green managed)"; return 0; }
  if [ -n "$want" ]; then target="$want"; else target="$(other_color "$cur")"; fi
  if [ "$cur" = "$target" ]; then log "$s already on $target — no-op"; return 0; fi
  # `addresses[*]` lists only ready IPs (notReadyAddresses excluded); a jsonpath on a missing
  # field EXITS 0 with empty output, so test the output is NON-EMPTY — an exit-code-only check
  # would sail past a not-ready endpoint.
  ready="$(kubectl -n "$NS" get endpoints "${s}-${target}" \
           -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  if [ -z "$ready" ]; then
    log "WARN: ${s}-${target} has no READY endpoints — NOT flipping $s (would cause an outage)"
    return 1
  fi
  if ! kubectl -n "$NS" patch svc "$s" --type merge \
        -p "{\"spec\":{\"selector\":{\"color\":\"${target}\"}}}" >/dev/null 2>&1; then
    log "ERROR: patch $s -> $target failed"; return 1
  fi
  log "flipped $s: ${cur} -> ${target}"
}

cmd_status() {
  have_kubectl
  local s c
  for s in "${SERVICES[@]}"; do
    c="$(kubectl -n "$NS" get svc "$s" -o jsonpath='{.spec.selector.color}' 2>/dev/null || true)"
    printf '%-14s %s\n' "$s" "${c:-<none>}"
  done
}

cmd_all() {
  have_kubectl
  local want="${1:-}" s failed=0
  [ -z "$want" ] || validate_color "$want"
  for s in "${SERVICES[@]}"; do flip_one "$s" "$want" || failed=1; done
  if [ "$failed" -ne 0 ]; then
    log "rollback finished with ERRORS — check each service's color (some may NOT have flipped)"
    return 1
  fi
  log "rollback complete — verify SLO recovery on the burn panel, then quarantine the bad digest"
}

cmd_service() {
  have_kubectl
  local s="$1" want="${2:-}"
  [ -z "$want" ] || validate_color "$want"
  flip_one "$s" "$want" || die "rollback of $s failed"
  log "rollback of $s complete — verify SLO recovery on the burn panel"
}

validate_color() {
  case "$1" in blue|green) : ;; *) die "color must be 'blue' or 'green', got '$1'";; esac
}

cmd_quarantine() {
  have_kubectl
  local digest="${1:?usage: rollback.sh quarantine <sha256-digest>}"
  case "$digest" in sha256:*) : ;; *) die "expected a sha256:... digest, got '$digest'";; esac
  kubectl -n "$NS" get configmap "$QUARANTINE_CM" >/dev/null 2>&1 \
    || kubectl -n "$NS" create configmap "$QUARANTINE_CM" >/dev/null
  kubectl -n "$NS" patch configmap "$QUARANTINE_CM" --type merge \
    -p "{\"data\":{\"${digest//:/_}\":\"quarantined $(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" >/dev/null
  # Honest scope: this RECORDS the quarantine in-cluster. Automatic enforcement (admission
  # refusing a quarantined digest) is the Kyverno policy (Phase 79); until then do NOT re-promote.
  log "quarantined $digest in ConfigMap ${QUARANTINE_CM} (namespace ${NS})."
  log "NOTE: enforcement is manual until the Phase-79 admission policy — do not re-promote it."
}

is_service() {
  local s
  for s in "${SERVICES[@]}"; do [ "$s" = "$1" ] && return 0; done
  return 1
}

main() {
  local cmd="${1:-status}"; shift || true
  case "$cmd" in
    status)     cmd_status ;;
    all)        cmd_all "${1:-}" ;;
    quarantine) cmd_quarantine "$@" ;;
    *)          if is_service "$cmd"; then cmd_service "$cmd" "${1:-}"; \
                else die "usage: rollback.sh {status|all [color]|<service> [color]|quarantine <digest>}"; fi ;;
  esac
}

if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then main "$@"; fi
