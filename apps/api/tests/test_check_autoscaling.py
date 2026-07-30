"""Phase 80 (Vol 9 §4.3, OPS-009): the autoscaling consistency gate. Pins the live config
AND re-breaks each cross-artifact invariant — a phantom HPA custom metric, a KEDA scaler
watching the wrong stream/group, a missing worker drain grace, drifted bounds."""

from __future__ import annotations

from typing import Any

import yaml
from scripts.check_autoscaling import BASE_DIR, _emitted_metric_names, autoscaling_errors

pytestmark: list[Any] = []

_STREAM = "stream:runs.execute"
_GROUP = "titan-runs-execute"


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


# a minimal healthy world: one custom-metric HPA + the KEDA worker + the two Deployments
_HPA_API = """
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: api}
spec:
  scaleTargetRef: {kind: Deployment, name: api}
  minReplicas: 3
  maxReplicas: 12
  metrics:
    - type: Resource
      resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
    - type: Pods
      pods:
        metric: {name: titan_http_requests_in_flight}
        target: {type: AverageValue, averageValue: "50"}
  behavior: {scaleDown: {stabilizationWindowSeconds: 300}}
"""
_SO_WORKER = """
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: worker}
spec:
  scaleTargetRef: {name: worker}
  minReplicaCount: 2
  maxReplicaCount: 50
  cooldownPeriod: 300
  triggers:
    - type: redis-streams
      metadata:
        address: redis:6379
        stream: "stream:runs.execute"
        consumerGroup: "titan-runs-execute"
        pendingEntriesCount: "10"
"""
_WORKLOADS = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec: {replicas: 3, template: {spec: {}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: worker}
spec: {replicas: 2, template: {spec: {terminationGracePeriodSeconds: 120}}}
"""

_EMITTED = {"titan_http_requests_in_flight", "titan_llm_calls_in_flight"}


def _errs(autoscale: str, workloads: str = _WORKLOADS) -> list[str]:
    return autoscaling_errors(
        _docs(autoscale), _docs(workloads), emitted=_EMITTED, exec_stream=_STREAM, exec_group=_GROUP
    )


# ---- the LIVE manifests + real app metrics/constants must pass -----------------


def test_live_autoscaling_is_consistent() -> None:
    from titan.events.streams import STREAM_RUNS_EXECUTE
    from titan.runs.coordinator import RUNS_EXECUTE_GROUP

    autoscale = (BASE_DIR / "autoscale.yaml").read_text(encoding="utf-8")
    workloads = (BASE_DIR / "workloads.yaml").read_text(encoding="utf-8")
    errs = autoscaling_errors(
        _docs(autoscale),
        _docs(workloads),
        emitted=_emitted_metric_names(),
        exec_stream=STREAM_RUNS_EXECUTE,
        exec_group=RUNS_EXECUTE_GROUP,
    )
    assert errs == []


def test_healthy_synthetic_world_passes() -> None:
    assert _errs(_HPA_API + "---\n" + _SO_WORKER) == []


# ---- re-break each invariant ---------------------------------------------------


def test_phantom_custom_metric_is_flagged() -> None:
    bad = _HPA_API.replace("titan_http_requests_in_flight", "titan_made_up_metric")
    assert any("not emitted by the app" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_keda_wrong_stream_is_flagged() -> None:
    bad = _SO_WORKER.replace("stream:runs.execute", "stream:wrong")
    assert any("STREAM_RUNS_EXECUTE" in e for e in _errs(_HPA_API + "---\n" + bad))


def test_keda_wrong_group_is_flagged() -> None:
    bad = _SO_WORKER.replace("titan-runs-execute", "wrong-group")
    assert any("RUNS_EXECUTE_GROUP" in e for e in _errs(_HPA_API + "---\n" + bad))


def test_missing_worker_grace_is_flagged() -> None:
    wl = _WORKLOADS.replace("terminationGracePeriodSeconds: 120", "foo: bar")
    assert any("terminationGracePeriodSeconds: 120" in e for e in _errs(_SO_WORKER, wl))


def test_drifted_bounds_are_flagged() -> None:
    bad = _HPA_API.replace("maxReplicas: 12", "maxReplicas: 99")
    assert any("§4.3" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_missing_scaledown_stabilisation_is_flagged() -> None:
    bad = _HPA_API.replace(
        "behavior: {scaleDown: {stabilizationWindowSeconds: 300}}", "behavior: {}"
    )
    assert any("stabilizationWindowSeconds" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_missing_keda_scaledobject_is_flagged() -> None:
    assert any("no KEDA ScaledObject" in e for e in _errs(_HPA_API))


def test_dropped_required_custom_metric_is_flagged() -> None:
    # F2: deleting api's Pods metric (regression to CPU-only) must be caught, not just a rename.
    cpu_only = """
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: api}
spec:
  scaleTargetRef: {kind: Deployment, name: api}
  minReplicas: 3
  maxReplicas: 12
  metrics:
    - type: Resource
      resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
  behavior: {scaleDown: {stabilizationWindowSeconds: 300}}
"""
    assert any("missing required Pods metric" in e for e in _errs(cpu_only + "---\n" + _SO_WORKER))


def test_drifted_pod_target_is_flagged() -> None:
    bad = _HPA_API.replace('averageValue: "50"', 'averageValue: "5000"')
    assert any("target" in e and "§4.3" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_drifted_cpu_util_is_flagged() -> None:
    bad = _HPA_API.replace("averageUtilization: 70", "averageUtilization: 50")
    assert any("CPU util" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_missing_cpu_metric_is_flagged() -> None:
    bad = """
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: frontend}
spec:
  scaleTargetRef: {kind: Deployment, name: frontend}
  minReplicas: 2
  maxReplicas: 6
  metrics: []
  behavior: {scaleDown: {stabilizationWindowSeconds: 300}}
"""
    assert any("missing a CPU Resource metric" in e for e in _errs(bad + "---\n" + _SO_WORKER))


def test_drifted_keda_pending_is_flagged() -> None:
    bad = _SO_WORKER.replace('pendingEntriesCount: "10"', 'pendingEntriesCount: "9999"')
    assert any("pendingEntriesCount" in e for e in _errs(_HPA_API + "---\n" + bad))
