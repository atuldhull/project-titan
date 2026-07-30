#!/usr/bin/env bash
# Behavioural unit test for the blue/green scripts (Phase 80 part 2, OPS-010/011). Blue/green is
# the classic script-error-prone class, so this drives release.sh, slo-watch.sh, and rollback.sh
# END-TO-END against a STATEFUL fake kubectl (Service colors persist across patches, so
# assert_serving is genuinely exercised) and asserts the load-bearing safety properties:
#   * the flip is exactly ONE Service-selector patch per HTTP tier (atomic, §5.1);
#   * the render renames+recolors+RETAGS the idle color from the live active one;
#   * retirement DELETES a color's autoscalers BEFORE scaling it to zero (F4 ordering);
#   * a CLEAN window retires the OLD color — but only after the NEW color is confirmed serving;
#   * a confirmed fast-burn rolls back to the OLD color EXPLICITLY, then retires the NEW color —
#     but only after the rollback is confirmed to have restored traffic;
#   * a WATCH failure (exit 2) retires NOTHING (both colors left up) — no fail-open outage;
#   * a rollback that CANNOT restore the old color retires NOTHING;
#   * a PARTIAL flip restores the old color and aborts;
#   * smoke failure aborts with no flip (injected-suite AND default-curl paths);
#   * slo-watch is a pure detector: exit 3 burn / 0 clean / 0 fail-safe when Prometheus is blind;
#   * rollback.sh flips one/all, honours an explicit color, and refuses a color with no ready pods.
#
# Requires bash + jq on PATH (the fake kubectl uses jq to parse applied YAML). No cluster.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="$HERE/release.sh"; ROLLBACK="$HERE/rollback.sh"; SLOWATCH="$HERE/slo-watch.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"

command -v jq >/dev/null || { echo "SKIP: jq not on PATH (required for the fake kubectl)"; exit 0; }

# ---- stateful fake kubectl ----------------------------------------------------
cat > "$BIN/kubectl" <<'FAKE'
#!/usr/bin/env bash
# State: $KSTATE/<svc> holds each Service's current selector color (persisted across patches).
# Env: FAKE_ACTIVE (default blue), FAKE_READY (default 1), FAKE_NOTREADY_COLOR (endpoints of that
# color report NO ready IPs), FAKE_PATCH_FAIL (patch of that svc fails).
orig="$*"
[ "${1:-}" = "-n" ] && shift 2
verb="${1:-}"; res="${2:-}"; name="${3:-}"
mkdir -p "$KSTATE" 2>/dev/null
svc_color() {
  if [ -f "$KSTATE/$1" ]; then cat "$KSTATE/$1"
  elif [ "$1" = worker ]; then printf ''
  else printf '%s' "${FAKE_ACTIVE:-blue}"; fi
}
dep_json() { local n="$1" c="${1%-*}"; cat <<J
{"kind":"Deployment","metadata":{"name":"$n","labels":{"app.kubernetes.io/component":"$c","color":"blue"},"uid":"u","resourceVersion":"7","generation":3,"creationTimestamp":"t","managedFields":[],"annotations":{"kubectl.kubernetes.io/last-applied-configuration":"{}"}},"spec":{"replicas":9,"selector":{"matchLabels":{"app.kubernetes.io/component":"$c","color":"blue"}},"template":{"metadata":{"labels":{"app.kubernetes.io/component":"$c","color":"blue"}},"spec":{"containers":[{"name":"$c","image":"ghcr.io/titan-platform/titan-$c:0.1.0"}]}}},"status":{"readyReplicas":9}}
J
}
hpa_json() { cat <<J
{"kind":"HorizontalPodAutoscaler","metadata":{"name":"$1","labels":{"color":"blue"},"uid":"u","resourceVersion":"7"},"spec":{"scaleTargetRef":{"apiVersion":"apps/v1","kind":"Deployment","name":"$1"},"minReplicas":3,"maxReplicas":12}}
J
}
so_json() { cat <<J
{"kind":"ScaledObject","metadata":{"name":"$1","labels":{"color":"blue"}},"spec":{"scaleTargetRef":{"name":"$1"},"minReplicaCount":2,"maxReplicaCount":50}}
J
}
case "$verb" in
  get)
    case "$res" in
      svc|service|services) svc_color "$name" ;;
      deploy|deployment|deployments) dep_json "$name" ;;
      hpa|horizontalpodautoscaler) hpa_json "$name" ;;
      scaledobject|scaledobjects) so_json "$name" ;;
      endpoints)
        ep_color="${name##*-}"
        if [ "$ep_color" = "${FAKE_NOTREADY_COLOR:-}" ]; then printf ''
        elif [ "${FAKE_READY:-1}" = "1" ]; then printf '10.1.2.3'
        else printf ''; fi ;;
    esac
    echo "$orig" >> "$KCALLS" ;;
  patch)
    if [ "$res" = svc ]; then
      if [ -n "${FAKE_PATCH_FAIL:-}" ] && [ "$name" = "$FAKE_PATCH_FAIL" ]; then
        echo "$orig" >> "$KCALLS"; exit 1
      fi
      c="$(printf '%s' "$orig" | sed -n 's/.*"color":"\([a-z]*\)".*/\1/p')"
      [ -n "$c" ] && printf '%s' "$c" > "$KSTATE/$name"
    fi
    echo "$orig" >> "$KCALLS" ;;
  apply)
    body="$(cat)"
    printf 'apply %s\n' "$(printf '%s' "$body" | jq -r '.kind+" "+.metadata.name+" "+((.spec.template.spec.containers[0].image)//(.spec.scaleTargetRef.name)//"-")' 2>/dev/null || echo PARSEFAIL)" >> "$KCALLS" ;;
  *) echo "$orig" >> "$KCALLS" ;;
esac
exit 0
FAKE
chmod +x "$BIN/kubectl"

cat > "$BIN/curl" <<'FAKE'
#!/usr/bin/env bash
[ "${FAKE_CURL_FAIL:-0}" = "1" ] && exit 22
body="${FAKE_PROM_BODY:-}"
[ -n "$body" ] || body='{"status":"success","data":{"result":[]}}'
printf '%s' "$body"
exit 0
FAKE
chmod +x "$BIN/curl"

mk() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }
mk smoke_ok 'exit 0'
mk smoke_fail 'exit 1'
mk slowatch_clean 'exit 0'
mk slowatch_burn 'exit 3'
mk slowatch_err 'exit 2'
mk rollback_stub 'echo "rollback $*" >> "$RBLOG"; exit 0'

export PATH="$BIN:$PATH"

# ---- assertions ---------------------------------------------------------------
pass=0; fail=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
chk() { if [ "$1" = "$2" ]; then ok "$3 ($1)"; else no "$3 (want $2 got $1)"; fi; }
nz() { if [ "$1" -ne 0 ]; then ok "$2 ($1)"; else no "$2 (want non-zero)"; fi; }
cnt() { grep -cE "$1" "$2" 2>/dev/null || true; }
firstline() { grep -nE "$1" "$2" 2>/dev/null | head -1 | cut -d: -f1; }
before() {
  local a b; a="$(firstline "$1" "$3")"; b="$(firstline "$2" "$3")"
  if [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; then ok "$4"; else no "$4 (A=$a B=$b)"; fi
}
# reset per-scenario state
newrun() { L="$TMP/$1.log"; : > "$L"; KS="$TMP/$1.state"; rm -rf "$KS"; mkdir -p "$KS"; }

# ===== A: clean release (blue -> green, SLO clean) =============================
echo "[A] clean release blue->green"; newrun a
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_ok" TITAN_SLOWATCH_CMD="$BIN/slowatch_clean" \
  bash "$RELEASE" 0.2.0 > "$TMP/a.out" 2>&1
chk "$?" 0 "release exits 0"
chk "$(cnt 'patch svc [a-z-]+ .*"color":"green"' "$L")" 4 "flip = 4 atomic patches to green"
chk "$(cnt 'patch svc worker' "$L")" 0 "worker Service NOT flipped (pull-based)"
chk "$(cnt 'apply Deployment [a-z-]+-green ghcr.io/titan-platform/titan-[a-z-]+:0.2.0' "$L")" 5 "5 green Deployments rendered at :0.2.0"
chk "$(cnt 'apply HorizontalPodAutoscaler [a-z-]+-green' "$L")" 3 "3 green HPAs rendered"
chk "$(cnt 'apply ScaledObject worker-green' "$L")" 1 "worker green ScaledObject rendered"
before 'delete (hpa|scaledobject) -l color=blue' 'scale deploy/[a-z-]+-blue --replicas=0' "$L" "retire old(blue): delete autoscalers BEFORE scale-0 (F4)"
chk "$(cnt 'scale deploy/[a-z-]+-blue --replicas=0' "$L")" 5 "old blue scaled to 0"
chk "$(cnt 'delete (hpa|scaledobject) -l color=green' "$L")" 0 "new green autoscalers NOT deleted"

# ===== B: fast-burn -> explicit rollback to blue, retire green =================
echo "[B] fast-burn -> rollback to blue, retire green"; newrun b
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_ok" TITAN_SLOWATCH_CMD="$BIN/slowatch_burn" \
  bash "$RELEASE" 0.2.0 > "$TMP/b.out" 2>&1
nz "$?" "release exits non-zero on fast-burn"
chk "$(cnt 'patch svc [a-z-]+ .*"color":"green"' "$L")" 4 "flip to green happened"
chk "$(cnt 'patch svc [a-z-]+ .*"color":"blue"' "$L")" 4 "explicit rollback flipped 4 tiers back to blue"
before 'delete (hpa|scaledobject) -l color=green' 'scale deploy/[a-z-]+-green --replicas=0' "$L" "retire NEW(green): delete BEFORE scale (F4)"
chk "$(cnt 'scale deploy/[a-z-]+-blue --replicas=0' "$L")" 0 "old blue NOT scaled down after rollback"

# ===== C: injected smoke failure -> no flip ===================================
echo "[C] injected smoke failure -> no flip"; newrun c
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_fail" TITAN_SLOWATCH_CMD="$BIN/slowatch_clean" \
  bash "$RELEASE" 0.2.0 > "$TMP/c.out" 2>&1
nz "$?" "release exits non-zero on smoke fail"
chk "$(cnt 'patch svc' "$L")" 0 "NO flip when smoke fails"
before 'delete (hpa|scaledobject) -l color=green' 'scale deploy/[a-z-]+-green --replicas=0' "$L" "failed idle(green) retired: delete BEFORE scale"

# ===== D: watch FAILS (exit 2) -> retire NOTHING (no fail-open outage) =========
echo "[D] slo-watch fails (exit 2) -> retire nothing"; newrun d
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_ok" TITAN_SLOWATCH_CMD="$BIN/slowatch_err" \
  bash "$RELEASE" 0.2.0 > "$TMP/d.out" 2>&1
nz "$?" "release exits non-zero when watch fails"
chk "$(cnt 'patch svc [a-z-]+ .*"color":"green"' "$L")" 4 "flip to green happened"
chk "$(cnt 'delete (hpa|scaledobject) -l color=' "$L")" 0 "NO color retired on watch failure"
chk "$(cnt 'scale deploy/[a-z-]+-(blue|green) --replicas=0' "$L")" 0 "NO color scaled to 0 (both left up)"

# ===== E: fast-burn but rollback CANNOT restore blue -> retire NOTHING =========
echo "[E] burn + rollback cannot restore blue -> retire nothing"; newrun e
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue FAKE_NOTREADY_COLOR=blue TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_ok" TITAN_SLOWATCH_CMD="$BIN/slowatch_burn" \
  bash "$RELEASE" 0.2.0 > "$TMP/e.out" 2>&1
nz "$?" "release exits non-zero"
chk "$(cnt 'scale deploy/[a-z-]+-green --replicas=0' "$L")" 0 "green NOT retired (rollback unverified)"
chk "$(cnt 'delete (hpa|scaledobject) -l color=green' "$L")" 0 "green autoscalers NOT deleted"

# ===== F: PARTIAL flip -> restore blue, retire green, abort ====================
echo "[F] partial flip -> restore blue, retire green"; newrun f
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue FAKE_PATCH_FAIL=orchestrator TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SMOKE_CMD="$BIN/smoke_ok" TITAN_SLOWATCH_CMD="$BIN/slowatch_clean" \
  bash "$RELEASE" 0.2.0 > "$TMP/f.out" 2>&1
nz "$?" "release aborts non-zero on partial flip"
chk "$(cnt 'patch svc [a-z-]+ .*"color":"blue"' "$L")" 3 "restore flipped the 3 succeeded tiers back to blue"
before 'delete (hpa|scaledobject) -l color=green' 'scale deploy/[a-z-]+-green --replicas=0' "$L" "idle(green) retired after restore: delete BEFORE scale"

# ===== G: DEFAULT curl smoke fails -> no flip =================================
echo "[G] default curl smoke fails -> no flip"; newrun g
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=blue FAKE_CURL_FAIL=1 TITAN_ROLLBACK_CMD="$ROLLBACK" \
  TITAN_SLOWATCH_CMD="$BIN/slowatch_clean" bash "$RELEASE" 0.2.0 > "$TMP/g.out" 2>&1
nz "$?" "default-smoke failure exits non-zero"
chk "$(cnt 'patch svc' "$L")" 0 "default-smoke failure: NO flip"

# ===== H/I/J: slo-watch detector (no rollback of its own) =====================
echo "[H] slo-watch burn -> exit 3, no rollback call"; RBLOG="$TMP/h.rb"; : > "$RBLOG"
RBLOG="$RBLOG" TITAN_ROLLBACK_CMD="$BIN/rollback_stub" TITAN_SLO_POLL_INTERVAL=1 TITAN_FAST_BURN_CONFIRM=1 \
  FAKE_PROM_BODY='{"status":"success","data":{"result":[{"metric":{},"value":[0,"0.5"]}]}}' \
  bash "$SLOWATCH" 3 > "$TMP/h.out" 2>&1
chk "$?" 3 "slo-watch exits 3 on confirmed burn"
chk "$(cnt 'rollback' "$RBLOG")" 0 "slo-watch does NOT roll back itself (detector only)"
echo "[I] slo-watch clean -> exit 0"
TITAN_SLO_POLL_INTERVAL=1 FAKE_PROM_BODY='{"status":"success","data":{"result":[]}}' \
  bash "$SLOWATCH" 1 > "$TMP/i.out" 2>&1
chk "$?" 0 "slo-watch exits 0 on a clean window"
echo "[J] prometheus down -> fail-safe exit 0"
TITAN_SLO_POLL_INTERVAL=1 FAKE_CURL_FAIL=1 bash "$SLOWATCH" 1 > "$TMP/j.out" 2>&1
chk "$?" 0 "slo-watch exits 0 (fail-safe) when Prometheus is unreachable"

# ===== K: rollback.sh single / all / explicit-color / no-ready-guard ==========
echo "[K] rollback.sh flips"; newrun k
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=green bash "$ROLLBACK" api > "$TMP/k1.out" 2>&1
chk "$(cnt 'patch svc api .*"color":"blue"' "$L")" 1 "rollback <service> toggles one service back"
newrun k2
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=green bash "$ROLLBACK" all blue > "$TMP/k2.out" 2>&1
chk "$(cnt 'patch svc [a-z-]+ .*"color":"blue"' "$L")" 4 "rollback all <color> forces the 4 HTTP tiers to blue"
chk "$(cnt 'patch svc worker' "$L")" 0 "rollback all skips the color-agnostic worker"
newrun k3
KCALLS="$L" KSTATE="$KS" FAKE_ACTIVE=green FAKE_NOTREADY_COLOR=blue bash "$ROLLBACK" api > "$TMP/k3.out" 2>&1
chk "$(cnt 'patch svc api' "$L")" 0 "rollback refuses to flip to a color with no ready endpoints"
echo "[K] rollback rejects a bad color arg"
if bash "$ROLLBACK" all purple > "$TMP/k4.out" 2>&1; then no "rollback should reject color 'purple'"; else ok "rollback rejects an invalid color"; fi

# ---- summary ------------------------------------------------------------------
echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
