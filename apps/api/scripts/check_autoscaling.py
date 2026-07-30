"""CI gate for the autoscaling manifests (Phase 80, Vol 9 §4.3, TITAN-OPS-009).

kubeconform validates HPA/KEDA schema; this validates the things it can't — the
cross-artifact consistency that silently breaks autoscaling if it drifts:

1. **Custom HPA metrics must be REAL app-emitted gauges.** An HPA `Pods` metric that names a
   gauge the app never exports never resolves — the HPA silently falls back to CPU-only. So
   every custom metric referenced must exist in the app's Prometheus registry (the Phase-79
   "don't reference what the app can't back" lesson, enforced).
2. **The KEDA worker scaler must watch the ACTUAL run-execute stream + consumer group.** If the
   code constants (STREAM_RUNS_EXECUTE / RUNS_EXECUTE_GROUP) drift from the ScaledObject
   metadata, the scaler reads the wrong (empty) lag and never scales.
3. **The worker must set terminationGracePeriodSeconds (graceful drain, OPS-009).**
4. **Each autoscaler's min must match its Deployment's baseline replicas** (an HPA/KEDA min
   below the baseline immediately scales the tier down at boot).
5. **The §4.3 bounds** (min/max per tier) are pinned so a silent edit is caught.

Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
BASE_DIR = _ROOT / "infra" / "k8s" / "base"

# §4.3 table: tier -> (min, max). Pinned so a silent bounds edit is caught.
_BOUNDS = {"frontend": (2, 6), "api": (3, 12), "llm-gateway": (2, 8), "worker": (2, 50)}
# HPAs that MUST carry a custom Pods metric with a specific target — so a regression to
# CPU-only (dropping the metric) is caught, not just a renamed one. tier -> (name, averageValue).
# (llm-gateway is CPU-only by design: its in-flight-LLM gauge is emitted in worker/api, not on
# its pods — see autoscale.yaml.)
_REQUIRED_POD_METRIC = {"api": ("titan_http_requests_in_flight", "50")}
_CPU_UTIL = 70  # §4.3 CPU target for every autoscaled web tier
_KEDA_PENDING = "10"  # §4.3 worker target: pending entries per replica


def _tier(name: str) -> str:
    """Strip the blue/green color suffix (§5.1) so a `<tier>-<color>` autoscaler/Deployment
    name maps back to its §4.3 tier for bounds/required-metric lookup."""
    return re.sub(r"-(blue|green)$", "", name)


def _docs(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]


def _emitted_metric_names() -> set[str]:
    """The Prometheus metric names the app actually registers (so an HPA can't reference a
    phantom gauge). Importing the metric modules is what registers the gauges on the default
    registry — do it explicitly so collect() sees them."""
    from prometheus_client import REGISTRY

    import titan.llm.metrics  # noqa: F401 — registers titan_llm_calls_in_flight
    import titan.observability.metrics  # noqa: F401 — registers titan_http_requests_in_flight

    return {metric.name for metric in REGISTRY.collect()}


def autoscaling_errors(
    autoscale: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    *,
    emitted: set[str],
    exec_stream: str,
    exec_group: str,
) -> list[str]:
    errors: list[str] = []
    hpas = [d for d in autoscale if d.get("kind") == "HorizontalPodAutoscaler"]
    scaledobjects = [d for d in autoscale if d.get("kind") == "ScaledObject"]

    # Deployment baseline replicas, by name.
    base_replicas: dict[str, int] = {}
    grace: dict[str, Any] = {}
    for d in workloads:
        if d.get("kind") == "Deployment":
            name = (d.get("metadata") or {}).get("name", "")
            spec = d.get("spec") or {}
            base_replicas[name] = spec.get("replicas", 1)
            grace[name] = ((spec.get("template") or {}).get("spec") or {}).get(
                "terminationGracePeriodSeconds"
            )

    # 1 + 4 + 5: HPA custom metrics real; bounds; min >= baseline; scaleDown stabilisation.
    for hpa in hpas:
        name = (hpa.get("metadata") or {}).get("name", "<no-name>")
        spec = hpa.get("spec") or {}
        target = (spec.get("scaleTargetRef") or {}).get("name", "") or name
        lo, hi = spec.get("minReplicas"), spec.get("maxReplicas")
        want = _BOUNDS.get(_tier(name))
        if want and (lo, hi) != want:
            errors.append(f"HPA {name}: bounds ({lo},{hi}) != §4.3 {want}")
        # baseline is keyed on the TARGET Deployment (not the HPA's own name) so a mis-keyed or
        # renamed HPA can't skip the "min >= the Deployment it scales" check.
        if target in base_replicas and lo is not None and lo < base_replicas[target]:
            errors.append(f"HPA {name}: minReplicas {lo} < baseline {base_replicas[target]}")
        scaledown = (spec.get("behavior") or {}).get("scaleDown") or {}
        if scaledown.get("stabilizationWindowSeconds") != 300:
            errors.append(f"HPA {name}: scaleDown stabilizationWindowSeconds must be 300 (§4.3)")
        cpu_ok = False
        pods_targets: dict[str, Any] = {}
        for metric in spec.get("metrics") or []:
            res = metric.get("resource") or {}
            if metric.get("type") == "Resource" and res.get("name") == "cpu":
                cpu_ok = True
                util = (res.get("target") or {}).get("averageUtilization")
                if util != _CPU_UTIL:
                    errors.append(f"HPA {name}: CPU util {util} != {_CPU_UTIL} (§4.3)")
            if metric.get("type") == "Pods":
                pods = metric.get("pods") or {}
                mname = (pods.get("metric") or {}).get("name", "")
                pods_targets[mname] = (pods.get("target") or {}).get("averageValue")
                if mname not in emitted:
                    errors.append(
                        f"HPA {name}: custom metric '{mname}' is not emitted by the app "
                        f"(HPA would silently fall back to CPU)"
                    )
        if not cpu_ok:
            errors.append(f"HPA {name}: missing a CPU Resource metric (§4.3)")
        # required custom metric must be PRESENT with the right target (not dropped → CPU-only).
        required = _REQUIRED_POD_METRIC.get(_tier(name))
        if required:
            rname, rval = required
            if rname not in pods_targets:
                errors.append(f"HPA {name}: missing required Pods metric '{rname}' (→ CPU-only)")
            elif pods_targets[rname] != rval:
                got = pods_targets[rname]
                errors.append(f"HPA {name}: '{rname}' target {got} != '{rval}' (§4.3)")

    # 2 + 3 + 4 + 5: the KEDA worker scaler.
    if not scaledobjects:
        errors.append("no KEDA ScaledObject for the worker (OPS-009 requires queue-lag scaling)")
    for so in scaledobjects:
        name = (so.get("metadata") or {}).get("name", "<no-name>")
        spec = so.get("spec") or {}
        target = (spec.get("scaleTargetRef") or {}).get("name", "")
        lo, hi = spec.get("minReplicaCount"), spec.get("maxReplicaCount")
        want = _BOUNDS.get(_tier(target))
        if want and (lo, hi) != want:
            errors.append(f"ScaledObject {name}: bounds ({lo},{hi}) != §4.3 {want} for {target}")
        if target in base_replicas and lo is not None and lo < base_replicas[target]:
            errors.append(
                f"ScaledObject {name}: min {lo} < {target} baseline {base_replicas[target]}"
            )
        if spec.get("cooldownPeriod") != 300:
            errors.append(f"ScaledObject {name}: cooldownPeriod must be 300 (scale-down)")
        if grace.get(target) != 120:
            errors.append(
                f"ScaledObject {name}: target Deployment {target} must set "
                f"terminationGracePeriodSeconds: 120 for graceful drain (OPS-009)"
            )
        redis_triggers = [t for t in spec.get("triggers") or [] if t.get("type") == "redis-streams"]
        if not redis_triggers:
            errors.append(f"ScaledObject {name}: no redis-streams trigger (queue lag, §4.3)")
        for trig in redis_triggers:
            meta = trig.get("metadata") or {}
            if meta.get("stream") != exec_stream:
                errors.append(
                    f"ScaledObject {name}: trigger stream '{meta.get('stream')}' != code "
                    f"STREAM_RUNS_EXECUTE '{exec_stream}'"
                )
            if meta.get("consumerGroup") != exec_group:
                errors.append(
                    f"ScaledObject {name}: trigger consumerGroup '{meta.get('consumerGroup')}' != "
                    f"code RUNS_EXECUTE_GROUP '{exec_group}'"
                )
            if meta.get("pendingEntriesCount") != _KEDA_PENDING:
                errors.append(
                    f"ScaledObject {name}: pendingEntriesCount "
                    f"'{meta.get('pendingEntriesCount')}' != '{_KEDA_PENDING}' (§4.3 target)"
                )
    return errors


def _all_base_docs() -> list[dict[str, Any]]:
    """Every YAML doc under base — so an HPA/ScaledObject/Deployment placed in any file (not just
    autoscale.yaml/workloads.yaml) is still bounds/metric/baseline checked (no scope-escape)."""
    out: list[dict[str, Any]] = []
    for path in sorted([*BASE_DIR.rglob("*.yaml"), *BASE_DIR.rglob("*.yml")]):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict):
                out.append(doc)
    return out


def main() -> None:
    from titan.events.streams import STREAM_RUNS_EXECUTE
    from titan.runs.coordinator import RUNS_EXECUTE_GROUP

    # autoscalers are filtered by kind and Deployments by kind, so passing the whole base doc set
    # as both arguments finds them wherever they live.
    docs = _all_base_docs()
    errors = autoscaling_errors(
        docs,
        docs,
        emitted=_emitted_metric_names(),
        exec_stream=STREAM_RUNS_EXECUTE,
        exec_group=RUNS_EXECUTE_GROUP,
    )
    if errors:
        sys.stderr.write("autoscaling check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        "autoscaling OK: custom metrics real, KEDA scaler matches code, bounds pinned.\n"
    )


if __name__ == "__main__":
    main()
