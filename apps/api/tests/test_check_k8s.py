"""Phase 79 (OPS-002/005/007): the K8s manifest security gate. Pins the live manifests
green AND re-breaks each invariant — including the selector/flow-style shapes that would
DODGE a naive gate (matchExpressions datastore target, a broad part-of selector, an
empty-from allow-all, an allow-all masquerading as default-deny, flow-style images)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.check_k8s import (
    K8S_DIR,
    collect_errors,
    image_tag_errors,
    networkpolicy_errors,
)

pytestmark: list[Any] = []


def _write(tmp: Path, name: str, body: str) -> list[Path]:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return [p]


# ---- live manifests are clean --------------------------------------------------


def test_live_manifests_pass() -> None:
    assert collect_errors(K8S_DIR) == []


# ---- image invariants ----------------------------------------------------------


def test_latest_tag_is_flagged(tmp_path: Path) -> None:
    files = _write(tmp_path, "d.yaml", "spec:\n  containers:\n    - image: ghcr.io/x/y:latest\n")
    assert any(":latest" in e for e in image_tag_errors(files))


def test_untagged_image_is_flagged(tmp_path: Path) -> None:
    files = _write(tmp_path, "d.yaml", "spec:\n  containers:\n    - image: ghcr.io/x/y\n")
    assert any("no tag or digest" in e for e in image_tag_errors(files))


def test_flow_style_latest_is_flagged(tmp_path: Path) -> None:
    # F3: a naive line-regex gate misses flow style; the YAML walk catches it.
    body = "spec:\n  containers: [{name: c, image: ghcr.io/x/y:latest}]\n"
    assert any(":latest" in e for e in image_tag_errors(_write(tmp_path, "d.yaml", body)))


def test_initcontainer_latest_is_flagged(tmp_path: Path) -> None:
    files = _write(tmp_path, "d.yaml", "spec:\n  initContainers:\n    - image: busybox:latest\n")
    assert any(":latest" in e for e in image_tag_errors(files))


def test_pinned_tag_and_digest_pass(tmp_path: Path) -> None:
    body = (
        "spec:\n  containers:\n"
        "    - image: ghcr.io/x/y:0.1.0\n"
        "    - image: redis:7.4-alpine\n"
        "    - image: ghcr.io/x/z@sha256:abc\n"
    )
    assert image_tag_errors(_write(tmp_path, "d.yaml", body)) == []


def test_registry_port_untagged_is_flagged_tagged_passes(tmp_path: Path) -> None:
    # the port colon must not be mistaken for a tag
    bad = _write(tmp_path, "b.yaml", "image: registry:5000/img\n")
    assert any("no tag or digest" in e for e in image_tag_errors(bad))
    ok = _write(tmp_path, "g.yaml", "image: registry:5000/img:1.2.3\n")
    assert image_tag_errors(ok) == []


def test_newtag_latest_is_flagged(tmp_path: Path) -> None:
    files = _write(tmp_path, "k.yaml", "images:\n  - name: ghcr.io/x/y\n    newTag: latest\n")
    assert any("newTag" in e for e in image_tag_errors(files))


def test_kyverno_pattern_refs_are_not_flagged(tmp_path: Path) -> None:
    body = "spec:\n  containers:\n    - image: '!*:latest'\n    - image: '*:*'\n"
    assert image_tag_errors(_write(tmp_path, "p.yaml", body)) == []


# ---- NetworkPolicy invariants --------------------------------------------------

_DEFAULT_DENY = (
    "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
    "metadata: {name: default-deny-ingress}\n"
    "spec: {podSelector: {}, policyTypes: [Ingress]}\n"
)


_NS_NGINX = (
    "        - namespaceSelector:\n"
    "            {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}\n"
)


def _np(target: str, from_block: str) -> str:
    return (
        "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
        f"metadata: {{name: p}}\nspec:\n  podSelector: {target}\n"
        f"  policyTypes: [Ingress]\n  ingress:\n    - from:\n{from_block}"
    )


def _errs(tmp_path: Path, body: str) -> list[str]:
    return networkpolicy_errors(_write(tmp_path, "np.yaml", body))


def test_missing_default_deny_is_flagged(tmp_path: Path) -> None:
    body = (
        "kind: NetworkPolicy\nmetadata: {name: x}\n"
        "spec: {podSelector: {matchLabels: {a: b}}, policyTypes: [Ingress]}\n"
    )
    assert any("default-deny" in e for e in networkpolicy_errors(_write(tmp_path, "np.yaml", body)))


def test_allow_all_masquerading_as_default_deny_is_flagged(tmp_path: Path) -> None:
    # podSelector:{} + ingress:[{}] must NOT count as default-deny (it allows everything).
    body = (
        "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: fake}\n"
        "spec: {podSelector: {}, policyTypes: [Ingress], ingress: [{}]}\n"
    )
    assert any("default-deny" in e for e in networkpolicy_errors(_write(tmp_path, "np.yaml", body)))


def test_datastore_matchlabels_namespace_is_flagged(tmp_path: Path) -> None:
    body = _DEFAULT_DENY + "---\n" + _np("{matchLabels: {cnpg.io/cluster: postgres}}", _NS_NGINX)
    errs = _errs(tmp_path, body)
    assert any("namespaceSelector" in e and "OPS-007" in e for e in errs)


def test_datastore_matchexpressions_namespace_is_flagged(tmp_path: Path) -> None:
    # F1: selecting redis via matchExpressions must NOT dodge the check.
    target = (
        "{matchExpressions: [{key: app.kubernetes.io/component, operator: In, values: [redis]}]}"
    )
    body = _DEFAULT_DENY + "---\n" + _np(target, "        - ipBlock: {cidr: 0.0.0.0/0}\n")
    assert any("ipBlock" in e for e in _errs(tmp_path, body))


def test_datastore_broad_partof_selector_is_flagged(tmp_path: Path) -> None:
    # F1: a broad `part-of: titan` selector also matches datastores.
    body = (
        _DEFAULT_DENY
        + "---\n"
        + _np("{matchLabels: {app.kubernetes.io/part-of: titan}}", _NS_NGINX)
    )
    assert any("OPS-007" in e for e in _errs(tmp_path, body))


def test_datastore_empty_from_allow_all_is_flagged(tmp_path: Path) -> None:
    body = (
        _DEFAULT_DENY + "---\napiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
        "metadata: {name: p}\n"
        "spec:\n  podSelector: {matchLabels: {app.kubernetes.io/component: redis}}\n"
        "  policyTypes: [Ingress]\n  ingress: [{}]\n"
    )
    assert any("empty from" in e for e in _errs(tmp_path, body))


def test_app_tier_from_ingress_nginx_is_ok(tmp_path: Path) -> None:
    # api/frontend legitimately accept ingress-nginx — must NOT be flagged.
    body = (
        _DEFAULT_DENY
        + "---\n"
        + _np("{matchLabels: {app.kubernetes.io/component: api}}", _NS_NGINX)
    )
    assert _errs(tmp_path, body) == []


def test_datastore_from_app_pods_is_ok(tmp_path: Path) -> None:
    frm = "        - podSelector: {matchLabels: {app.kubernetes.io/component: api}}\n"
    body = _DEFAULT_DENY + "---\n" + _np("{matchLabels: {cnpg.io/cluster: postgres}}", frm)
    assert _errs(tmp_path, body) == []
