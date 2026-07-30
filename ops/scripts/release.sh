#!/usr/bin/env bash
# Blue/green release engine (Vol 9 §5.1, TITAN-OPS-010/011). Deploys a new version to the IDLE
# color, smoke-tests it on its preview Service, flips the stable Service selector atomically,
# watches the SLO burn for 30 min, then retires the losing color.
#
#   ops/scripts/release.sh 0.2.0            # roll out tag 0.2.0 to the idle color
#   ops/scripts/release.sh @sha256:abc123…  # roll out a pinned digest
#
# Kubernetes operation — needs kubectl (cluster context) + jq. Runs in-cluster as a Job or from
# a jump host with cluster DNS/network for smoke.
#
# STEPS: (1) render idle-color Deployments from the live active color at the new image; (2) render
# the idle-color autoscalers (idle color protected before it takes traffic); (3) wait rollout;
# (4) smoke the idle preview Services; (5) FLIP each stable Service selector.color (ONE atomic
# patch per tier); (6) watch the SLO via slo-watch.sh — a pure DETECTOR returning 0=clean,
# 3=confirmed fast-burn, other=watch-failed; (7a) clean → retire the OLD color; (7b) burn → roll
# back to the OLD color explicitly, then retire the NEW color; (7c) watch failed → retire NOTHING.
#
# SAFETY INVARIANTS (a prior design scaled the LIVE color to zero on a mere missing-binary exit —
# an outage; these remove that):
#   * A destructive retire of a color is ALWAYS gated on `assert_serving <the-other-color>` — the
#     surviving color must actually be selected AND have ready endpoints first.
#   * slo-watch is a detector; THIS script does the rollback, with the EXPLICIT known-good color.
#     A non-zero watch exit is NOT assumed to mean "rollback already happened."
#   * Retire = delete a color's autoscalers FIRST, THEN scale its Deployments to 0 (never the
#     reverse, or the HPA re-inflates the color being retired — the Phase-80-part-1 F4 collision).
#   * WORKER caveat (honest): both worker colors share the ONE Redis consumer group
#     (titan-runs-execute is a code constant, not per-color), so the new worker color begins
#     consuming/acking tasks at deploy (step 1), before the flip, and both colors co-process
#     during the window. Correctness holds (at-least-once + idempotent guarded updates + a single
#     leader for ordering-sensitive loops), but a BAD new worker version processes tasks until it
#     is retired at 7b. Per-color consumer groups (§5.1's "switch which color's group is scaled")
#     require an app change and are tracked as future work.
set -euo pipefail

NS="${TITAN_NAMESPACE:-titan-prod}"
# All stateless tiers get a per-color Deployment; only the HTTP tiers flip via a Service.
STATELESS=(${TITAN_STATELESS:-frontend api orchestrator worker llm-gateway})
HTTP_SERVICES=(${TITAN_HTTP_SERVICES:-frontend api orchestrator llm-gateway})
HPA_TIERS=(${TITAN_HPA_TIERS:-frontend api llm-gateway})   # CPU/custom-metric HPAs
WATCH_SECONDS="${TITAN_WATCH_SECONDS:-1800}"               # §5.1 30-minute observation
ROLLOUT_TIMEOUT="${TITAN_ROLLOUT_TIMEOUT:-300s}"
SLOWATCH_CMD="${TITAN_SLOWATCH_CMD:-$(dirname "$0")/slo-watch.sh}"
ROLLBACK_CMD="${TITAN_ROLLBACK_CMD:-$(dirname "$0")/rollback.sh}"

log()  { printf '%s [release] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die()  { log "FATAL: $*"; exit 1; }
have() { command -v "$1" >/dev/null || die "$1 not found (required)"; }

other_color() { [ "$1" = "blue" ] && echo green || echo blue; }
deploy_name() { echo "$1-$2"; }  # <component>-<color>

# §4.3 baseline replicas per tier (what the idle color starts at, and what its HPA min holds).
baseline() {
  case "$1" in
    api) echo 3 ;;
    orchestrator) echo 1 ;;
    frontend|worker|llm-gateway) echo 2 ;;
    *) echo 1 ;;
  esac
}

# The active color = whatever the `api` stable Service selects. DIE on a read error (wrong
# kube-context / RBAC / apiserver blip): guessing "blue" here could apply the new image straight
# onto the live color and bypass staging. Empty (Service exists without a color selector, i.e. a
# pre-blue/green cluster) legitimately defaults to blue.
active_color() {
  local c
  if ! c="$(kubectl -n "$NS" get svc api -o jsonpath='{.spec.selector.color}' 2>/dev/null)"; then
    die "cannot read active color from Service 'api' — refusing to guess the live color"
  fi
  echo "${c:-blue}"
}

# image tag/digest suffix from the version arg.
suffix_for() { case "$1" in @sha256:*) echo "$1" ;; *) echo ":$1" ;; esac; }

# Render <comp>'s idle-color Deployment from the live active-color one at the new image.
render_deploy() {
  local comp="$1" from="$2" to="$3" suffix="$4" reps
  reps="$(baseline "$comp")"
  kubectl -n "$NS" get deploy "$(deploy_name "$comp" "$from")" -o json \
    | jq --arg color "$to" --arg name "$(deploy_name "$comp" "$to")" \
         --arg suffix "$suffix" --argjson reps "$reps" '
        .metadata.name = $name
        | .metadata.labels.color = $color
        | .spec.selector.matchLabels.color = $color
        | .spec.template.metadata.labels.color = $color
        | .spec.replicas = $reps
        | .spec.template.spec.containers[].image
            |= (sub("(@sha256:[0-9a-fA-F]+|:[^/@]+)$"; "") + $suffix)
        | del(.status, .metadata.uid, .metadata.resourceVersion, .metadata.generation,
              .metadata.creationTimestamp, .metadata.managedFields, .metadata.annotations)
      ' \
    | kubectl -n "$NS" apply -f - >/dev/null
  log "rendered $(deploy_name "$comp" "$to") @ ${suffix#[:@]} (${reps} replicas)"
}

# Render <comp>'s idle-color autoscaler from the active one (HPA for web tiers; the worker's is a
# KEDA ScaledObject). Retarget keeps it pointed at the concrete colored Deployment (F4) and
# re-labels it so retirement finds it by color.
render_autoscaler() {
  local comp="$1" from="$2" to="$3" kind name_from name_to
  if [ "$comp" = "worker" ]; then kind="scaledobject"; else kind="hpa"; fi
  name_from="$(deploy_name "$comp" "$from")"; name_to="$(deploy_name "$comp" "$to")"
  kubectl -n "$NS" get "$kind" "$name_from" -o json \
    | jq --arg color "$to" --arg name "$name_to" '
        .metadata.name = $name
        | .metadata.labels.color = $color
        | .spec.scaleTargetRef.name = $name
        | del(.status, .metadata.uid, .metadata.resourceVersion, .metadata.generation,
              .metadata.creationTimestamp, .metadata.managedFields, .metadata.annotations)
      ' \
    | kubectl -n "$NS" apply -f - >/dev/null
  log "rendered ${kind}/${name_to} -> ${name_to}"
}

wait_rollout() {
  local comp="$1" color="$2"
  kubectl -n "$NS" rollout status "deploy/$(deploy_name "$comp" "$color")" \
    --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  log "rollout ok: $(deploy_name "$comp" "$color")"
}

# Smoke the idle color through its preview Service. MUST return non-zero (never die/exit) so the
# caller's `if ! smoke` cleanup runs. Overridable via TITAN_SMOKE_CMD "<color>"; the default
# curls each HTTP preview's health endpoint and FAILS CLOSED.
smoke() {
  local color="$1" comp host port path
  if [ -n "${TITAN_SMOKE_CMD:-}" ]; then "$TITAN_SMOKE_CMD" "$color"; return; fi
  command -v curl >/dev/null || { log "curl not found — cannot smoke ${color}"; return 1; }
  for comp in "${HTTP_SERVICES[@]}"; do
    host="${comp}-${color}.${NS}.svc"
    case "$comp" in frontend) port=80; path="/" ;; *) port=8000; path="/readyz" ;; esac
    log "smoke http://${host}:${port}${path}"
    if ! curl -fsS --max-time 10 "http://${host}:${port}${path}" >/dev/null; then
      log "smoke FAILED on ${comp}-${color}"; return 1
    fi
  done
}

# The flip: ONE selector patch per HTTP Service. Attempts ALL tiers even if one fails (returns
# non-zero if any did) so a transient error can't leave us stuck exactly half-flipped. Workers
# are pull-based (no Service flip).
flip() {
  local color="$1" comp failed=0
  for comp in "${HTTP_SERVICES[@]}"; do
    if kubectl -n "$NS" patch svc "$comp" --type merge \
         -p "{\"spec\":{\"selector\":{\"color\":\"${color}\"}}}" >/dev/null 2>&1; then
      log "flipped $comp -> $color"
    else
      log "ERROR: flip $comp -> $color failed"; failed=1
    fi
  done
  return "$failed"
}

# A color is "serving" iff EVERY HTTP tier's stable Service selects it AND its pods have ready
# endpoints. Every destructive retire is gated on the OTHER color serving first.
assert_serving() {
  local color="$1" comp sel ready
  for comp in "${HTTP_SERVICES[@]}"; do
    sel="$(kubectl -n "$NS" get svc "$comp" -o jsonpath='{.spec.selector.color}' 2>/dev/null || true)"
    if [ "$sel" != "$color" ]; then
      log "assert_serving: svc $comp selects '${sel:-<none>}', not ${color}"; return 1
    fi
    ready="$(kubectl -n "$NS" get endpoints "${comp}-${color}" \
             -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
    if [ -z "$ready" ]; then log "assert_serving: ${comp}-${color} has no READY endpoints"; return 1; fi
  done
  return 0
}

# Retire a color: DELETE its autoscalers FIRST (so nothing re-inflates it — F4), THEN scale its
# Deployments to 0. Surface a failed scale instead of masking it.
retire_color() {
  local color="$1" comp
  kubectl -n "$NS" delete hpa -l "color=${color}" --ignore-not-found >/dev/null
  kubectl -n "$NS" delete scaledobject -l "color=${color}" --ignore-not-found >/dev/null
  log "dropped autoscalers for color=${color}"
  for comp in "${STATELESS[@]}"; do
    if ! kubectl -n "$NS" scale "deploy/$(deploy_name "$comp" "$color")" --replicas=0 >/dev/null 2>&1; then
      log "WARN: scale ${comp}-${color} to 0 failed (may be absent) — verify it is not running"
    fi
  done
  log "scaled color=${color} to zero"
}

main() {
  have kubectl; have jq
  local version="${1:?usage: release.sh <version|@sha256:digest>}"
  local suffix active idle comp rc
  suffix="$(suffix_for "$version")"
  active="$(active_color)"; idle="$(other_color "$active")"
  log "active=${active} idle=${idle} version=${version} ns=${NS}"

  # 1 + 2: deploy the idle color and its autoscalers.
  for comp in "${STATELESS[@]}"; do render_deploy "$comp" "$active" "$idle" "$suffix"; done
  for comp in "${HPA_TIERS[@]}"; do render_autoscaler "$comp" "$active" "$idle"; done
  render_autoscaler worker "$active" "$idle"

  # 3: wait for the idle color to be Ready.
  for comp in "${STATELESS[@]}"; do wait_rollout "$comp" "$idle"; done

  # 4: smoke the idle color BEFORE any traffic. Failure ⇒ no flip; retire the idle color; abort.
  if ! smoke "$idle"; then
    log "smoke failed — retiring idle color ${idle}, aborting (no flip)"
    retire_color "$idle"
    exit 1
  fi

  # 5: atomic flip. On a PARTIAL flip (version skew across tiers), restore the still-warm active
  #    color everywhere, then retire the idle color ONLY if active is confirmed serving.
  if ! flip "$idle"; then
    log "flip incomplete — restoring ${active} on all services"
    "$ROLLBACK_CMD" all "$active" || log "WARN: restore to ${active} reported errors"
    if assert_serving "$active"; then retire_color "$idle"; fi
    die "release aborted on a partial flip (attempted restore to ${active}) — verify colors"
  fi
  log "FLIP COMPLETE ${active} -> ${idle}; watching SLO for ${WATCH_SECONDS}s"

  # 6: observe. slo-watch is a DETECTOR: 0=clean, 3=confirmed burn, other=watch failed.
  set +e; watch_slo; rc=$?; set -e

  if [ "$rc" -eq 0 ]; then
    # 7a: success — retire the OLD color, but only after confirming the NEW color truly serves.
    if ! assert_serving "$idle"; then
      die "SLO clean but ${idle} is not serving on all tiers — NOT retiring ${active} (both up); INTERVENE"
    fi
    log "SLO clean for the window — retiring old color ${active}"
    retire_color "$active"
    log "release ${version} complete; active color is now ${idle}"
  elif [ "$rc" -eq 3 ]; then
    # 7b: confirmed fast-burn — roll back to the KNOWN-GOOD active color explicitly, and retire
    #     the failed new color ONLY after confirming the rollback actually restored traffic.
    log "fast-burn CONFIRMED — rolling back to ${active}"
    "$ROLLBACK_CMD" all "$active" || log "WARN: rollback to ${active} reported errors"
    if ! assert_serving "$active"; then
      die "rollback did not restore ${active} on all tiers — NOT retiring ${idle} (both up); INTERVENE"
    fi
    log "rolled back to ${active}; retiring failed color ${idle}"
    retire_color "$idle"
    exit 1
  else
    # 7c: the watch itself failed (e.g. missing curl/jq → exit 2). Do NOT infer a burn and do NOT
    #     retire any color — leave BOTH up (2× capacity, safe) and page for manual judgement.
    die "SLO watch failed (exit ${rc}) — cannot judge the window; left BOTH colors up, INTERVENE"
  fi
}

watch_slo() { "$SLOWATCH_CMD" "$WATCH_SECONDS"; }

# Sourceable for unit tests; run main only when executed.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then main "$@"; fi
