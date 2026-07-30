#!/usr/bin/env bash
# Post-flip SLO DETECTOR (Vol 9 §5.2, TITAN-OPS-011). Polls Prometheus for the SAME fast-burn
# condition that pages (APIErrorBudgetFastBurn in infra/prometheus/rules/titan-slo.yml). It only
# DETECTS — it never flips anything itself:
#     exit 0  → clean window (or burn could not be CONFIRMED)
#     exit 3  → fast-burn CONFIRMED (caller must roll back)
#     exit 2  → precondition failure (curl/jq missing) — caller must NOT infer a burn
# release.sh drives the rollback with the EXPLICIT last-known-good color it holds; making this a
# pure detector is deliberate — an earlier design let the release engine treat ANY non-zero as
# "burn + rollback done" and scale the LIVE color to zero on a mere missing-binary exit (an
# outage). Detector-with-typed-exit-codes removes that fail-open.
#
#   ops/scripts/slo-watch.sh 1800     # watch for 30 min (default); prints result, sets exit code
#
# FAIL-SAFE: Prometheus unreachable / no data is NOT a burn (a monitoring outage must never by
# itself cause a revert); it resets the confirm streak, exactly mirroring Prometheus's own
# `for: 2m` semantics (a data gap resets the pending alert). The human is paged by the real
# Prometheus alert regardless, so erring toward "don't auto-act" is the safe default.
set -euo pipefail

WINDOW="${1:-${TITAN_WATCH_SECONDS:-1800}}"
PROM_URL="${TITAN_PROM_URL:-http://prometheus.titan-prod.svc:9090}"
POLL_INTERVAL="${TITAN_SLO_POLL_INTERVAL:-30}"
FAST_BURN_FOR="${TITAN_FAST_BURN_FOR:-120}"          # matches the alert's `for: 2m`

# Consecutive breaching polls to confirm. N polls span (N-1)*poll seconds, so to cover the full
# `for` window we need ceil(for/poll)+1 samples (an off-by-one the other way fires ~1 poll early).
CONFIRM="${TITAN_FAST_BURN_CONFIRM:-$(( (FAST_BURN_FOR + POLL_INTERVAL - 1) / POLL_INTERVAL + 1 ))}"
[ "$CONFIRM" -ge 1 ] 2>/dev/null || CONFIRM=1

# The fast-burn boolean — byte-for-byte the APIErrorBudgetFastBurn expr (14.4× the 0.1% budget
# over BOTH 1h and 5m). Non-empty result set ⇒ burning. Keep in sync with titan-slo.yml.
FAST_BURN_EXPR='job:titan_api_error_ratio:rate1h > (14.4 * 0.001) and job:titan_api_error_ratio:rate5m > (14.4 * 0.001)'

log() { printf '%s [slo-watch] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

# Prints "burn" / "clean" / "unknown". "unknown" = Prometheus down or malformed (fail-safe).
probe() {
  local body
  if ! body="$(curl -fsS -G "$PROM_URL/api/v1/query" \
        --data-urlencode "query=${FAST_BURN_EXPR}" --max-time 10 2>/dev/null)"; then
    echo unknown; return
  fi
  case "$(printf '%s' "$body" | jq -r '.status // "error"' 2>/dev/null)" in
    success) : ;;
    *) echo unknown; return ;;
  esac
  if printf '%s' "$body" | jq -e '.data.result | length > 0' >/dev/null 2>&1; then
    echo burn
  else
    echo clean
  fi
}

main() {
  command -v curl >/dev/null || { log "FATAL: curl not found"; exit 2; }
  command -v jq   >/dev/null || { log "FATAL: jq not found"; exit 2; }
  log "watching ${PROM_URL} for fast-burn; window=${WINDOW}s poll=${POLL_INTERVAL}s confirm=${CONFIRM}"
  local start now consec=0 state
  start="$(date +%s)"
  while :; do
    state="$(probe)"
    case "$state" in
      burn)
        consec=$(( consec + 1 ))
        log "fast-burn observed (${consec}/${CONFIRM})"
        if [ "$consec" -ge "$CONFIRM" ]; then
          log "CONFIRMED fast-burn — signalling caller to roll back (exit 3)"
          exit 3
        fi
        ;;
      clean) consec=0 ;;
      unknown) consec=0; log "WARN: Prometheus unreachable/no-data — cannot judge burn (watch manually)" ;;
    esac
    now="$(date +%s)"
    [ "$(( now - start ))" -ge "$WINDOW" ] && break
    sleep "$POLL_INTERVAL"
  done
  log "SLO window clean (${WINDOW}s) — no rollback needed"
  exit 0
}

if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then main "$@"; fi
