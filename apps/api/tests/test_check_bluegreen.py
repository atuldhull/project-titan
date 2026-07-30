"""Phase 80 part 2 (Vol 9 §5.1, OPS-010/011): the blue/green topology gate. Pins the LIVE
base manifests AND re-breaks each cross-object invariant — an un-suffixed Deployment, a
color/selector mismatch, a stable Service selecting an absent color, a missing/mis-pinned
preview Service, a colored worker Service, and (the F4 fix) an autoscaler whose target won't
resolve / isn't color-labelled / is committed for an idle color."""

from __future__ import annotations

from typing import Any

from scripts.check_bluegreen import BASE_DIR, _docs_from_dir, bluegreen_errors

pytestmark: list[Any] = []

_HTTP = ["frontend", "api", "orchestrator", "llm-gateway"]
_ALL = _HTTP + ["worker"]


def _deploy(comp: str, color: str, replicas: int = 2) -> dict[str, Any]:
    labels = {"app.kubernetes.io/component": comp, "color": color}
    return {
        "kind": "Deployment",
        "metadata": {"name": f"{comp}-{color}", "labels": {"color": color}},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": dict(labels)},
            "template": {"metadata": {"labels": dict(labels)}, "spec": {}},
        },
    }


def _svc(name: str, comp: str, color: str | None = None) -> dict[str, Any]:
    sel: dict[str, Any] = {"app.kubernetes.io/component": comp}
    if color is not None:
        sel["color"] = color
    return {"kind": "Service", "metadata": {"name": name}, "spec": {"selector": sel}}


def _autoscaler(kind: str, comp: str, color: str, target: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "metadata": {"name": f"{comp}-{color}", "labels": {"color": color}},
        "spec": {"scaleTargetRef": {"name": target if target is not None else f"{comp}-{color}"}},
    }


def _healthy() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = [_deploy(c, "blue") for c in _ALL]
    for comp in _HTTP:
        docs.append(_svc(comp, comp, "blue"))  # stable, active = blue
        docs.append(_svc(f"{comp}-blue", comp, "blue"))
        docs.append(_svc(f"{comp}-green", comp, "green"))
    docs.append(_svc("worker", "worker"))  # color-agnostic (pull-based)
    for comp in ["frontend", "api", "llm-gateway"]:
        docs.append(_autoscaler("HorizontalPodAutoscaler", comp, "blue"))
    docs.append(_autoscaler("ScaledObject", "worker", "blue"))
    return docs


def _find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for d in docs:
        if d.get("kind") == kind and (d.get("metadata") or {}).get("name") == name:
            return d
    raise AssertionError(f"{kind}/{name} not in fixture")


# ---- the LIVE base manifests must pass -----------------------------------------


def test_live_bluegreen_is_consistent() -> None:
    assert bluegreen_errors(_docs_from_dir(BASE_DIR)) == []


def test_healthy_synthetic_world_passes() -> None:
    assert bluegreen_errors(_healthy()) == []


# ---- re-break each invariant ---------------------------------------------------


def test_unsuffixed_deployment_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "Deployment", "api-blue")["metadata"]["name"] = "api"
    assert any("color-suffixed" in e for e in bluegreen_errors(docs))


def test_selector_color_mismatch_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "Deployment", "api-blue")["spec"]["selector"]["matchLabels"]["color"] = "green"
    assert any("selector color" in e for e in bluegreen_errors(docs))


def test_missing_selector_color_is_flagged() -> None:
    docs = _healthy()
    del _find(docs, "Deployment", "api-blue")["spec"]["selector"]["matchLabels"]["color"]
    assert any("'color' missing from selector" in e for e in bluegreen_errors(docs))


def test_template_color_mismatch_is_flagged() -> None:
    docs = _healthy()
    dep = _find(docs, "Deployment", "api-blue")
    dep["spec"]["template"]["metadata"]["labels"]["color"] = "green"
    assert any("pod-template color" in e for e in bluegreen_errors(docs))


def test_stable_service_without_color_is_flagged() -> None:
    docs = _healthy()
    del _find(docs, "Service", "api")["spec"]["selector"]["color"]
    assert any("must pin a 'color'" in e for e in bluegreen_errors(docs))


def test_stable_service_selecting_absent_color_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "Service", "api")["spec"]["selector"]["color"] = "green"  # no green Deployment
    assert any("no api-green Deployment exists" in e for e in bluegreen_errors(docs))


def test_missing_preview_service_is_flagged() -> None:
    docs = [d for d in _healthy() if (d.get("metadata") or {}).get("name") != "api-green"]
    assert any("api-green" in e and "preview" in e for e in bluegreen_errors(docs))


def test_mispinned_preview_service_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "Service", "api-green")["spec"]["selector"]["color"] = "blue"
    assert any("preview must pin color 'green'" in e for e in bluegreen_errors(docs))


def test_colored_worker_service_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "Service", "worker")["spec"]["selector"]["color"] = "blue"
    assert any("color-AGNOSTIC" in e for e in bluegreen_errors(docs))


def test_autoscaler_bare_target_is_flagged() -> None:
    # F4: a bare `scaleTargetRef: api` won't resolve once Deployments are api-<color>.
    docs = _healthy()
    _find(docs, "HorizontalPodAutoscaler", "api-blue")["spec"]["scaleTargetRef"]["name"] = "api"
    assert any("matches no Deployment" in e for e in bluegreen_errors(docs))


def test_autoscaler_missing_color_label_is_flagged() -> None:
    docs = _healthy()
    del _find(docs, "HorizontalPodAutoscaler", "api-blue")["metadata"]["labels"]["color"]
    assert any("missing 'color' label" in e for e in bluegreen_errors(docs))


def test_autoscaler_color_label_mismatch_is_flagged() -> None:
    docs = _healthy()
    _find(docs, "HorizontalPodAutoscaler", "api-blue")["metadata"]["labels"]["color"] = "green"
    errs = bluegreen_errors(docs)
    assert any("color label 'green' != target color 'blue'" in e for e in errs)


def test_committed_green_autoscaler_is_flagged() -> None:
    # A committed green autoscaler would re-inflate the idle color on `apply -k` (F4).
    docs = _healthy()
    docs.append(_deploy("api", "green"))  # target resolves; color-commit rule still bites
    docs.append(_autoscaler("HorizontalPodAutoscaler", "api", "green"))
    errs = bluegreen_errors(docs)
    assert any("only the active (blue) autoscaler may be committed" in e for e in errs)


def test_autoscaler_name_target_mismatch_is_flagged() -> None:
    # api-blue HPA pointed at frontend-blue (both exist, same color) would scale the wrong tier
    # under api's rules — the 1:1 name==target rule must catch it.
    docs = _healthy()
    hpa = _find(docs, "HorizontalPodAutoscaler", "api-blue")
    hpa["spec"]["scaleTargetRef"]["name"] = "frontend-blue"
    assert any("name must equal scaleTargetRef" in e for e in bluegreen_errors(docs))


def test_mispinned_preview_component_is_flagged() -> None:
    docs = _healthy()
    svc = _find(docs, "Service", "api-green")
    svc["spec"]["selector"]["app.kubernetes.io/component"] = "frontend"
    assert any("must select component 'api'" in e for e in bluegreen_errors(docs))


def test_missing_stable_service_is_flagged() -> None:
    docs = [
        d
        for d in _healthy()
        if not (d.get("kind") == "Service" and (d.get("metadata") or {}).get("name") == "api")
    ]
    assert any("stable Service is missing" in e for e in bluegreen_errors(docs))


def test_matchexpressions_deployment_cannot_dodge_color_check() -> None:
    # F1: a stateless Deployment must be color-checked even if its selector uses matchExpressions
    # (component identified from metadata.labels). Here color is absent from matchLabels → flagged.
    docs = _healthy()
    dep = _find(docs, "Deployment", "worker-blue")
    dep["spec"]["selector"] = {
        "matchExpressions": [{"key": "color", "operator": "In", "values": ["blue"]}]
    }
    assert any("worker-blue" in e and "selector" in e for e in bluegreen_errors(docs))


def test_worker_deployment_must_be_color_suffixed() -> None:
    docs = _healthy()
    _find(docs, "Deployment", "worker-blue")["metadata"]["name"] = "worker"
    # a bare `worker` Deployment also breaks the ScaledObject target resolution
    errs = bluegreen_errors(docs)
    assert any("color-suffixed" in e for e in errs)
