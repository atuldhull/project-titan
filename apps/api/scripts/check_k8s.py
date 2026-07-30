"""CI gate for the K8s manifests' SECURITY posture (Phase 79, TITAN-OPS-002/005/007).

kubeconform validates schema; this validates two things it cannot, over the committed
`infra/k8s` manifests:

1. **No floating images (OPS-002/005):** no `image:`/`imageName:`/`newTag:` uses `:latest`
   or an untagged ref (untagged defaults to :latest). Pinned tags or @sha256 digests only.
   Images are found by walking the parsed YAML (block AND flow style, initContainers, CNPG
   imageName, kustomize newTag) — not a line regex that flow style would slip past.
2. **NetworkPolicy posture (OPS-007):** a TRUE default-deny-ingress exists (empty podSelector,
   Ingress, and NO ingress rules), AND no policy that could select a datastore
   (Postgres/Redis/Qdrant) admits ingress from another namespace, an IP block, or "all
   sources" (empty `from`). "Could select a datastore" accounts for matchLabels, a bare
   `part-of: titan` / empty selector, and matchExpressions on the component label — so the
   check can't be dodged by picking a selector shape it doesn't recognise.

Pure filesystem + YAML — no cluster, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

K8S_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s"

_DATASTORE_COMPONENTS = {"postgres", "redis", "redis-sentinel", "qdrant"}


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _docs(files: list[Path]) -> list[Any]:
    out: list[Any] = []
    for path in files:
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc is not None:
                out.append(doc)
    return out


# ---- images -------------------------------------------------------------------


def _walk_images(node: Any, images: list[str], newtags: list[str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("image", "imageName") and isinstance(val, str):
                images.append(val)
            elif key == "newTag":
                newtags.append(str(val))
            else:
                _walk_images(val, images, newtags)
    elif isinstance(node, list):
        for item in node:
            _walk_images(item, images, newtags)


def _ref_is_floating(ref: str) -> str | None:
    if "*" in ref or "!" in ref:
        return None  # a Kyverno match/validate PATTERN, not a concrete image ref
    if ":latest" in ref:
        return "uses :latest"
    if "@sha256:" in ref:
        return None
    tail = ref.rsplit("/", 1)[-1]  # last path segment holds the [:tag]; registry:port is earlier
    if ":" not in tail:
        return "has no tag or digest (floats on :latest)"
    return None


def image_tag_errors(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        images: list[str] = []
        newtags: list[str] = []
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc is not None:
                _walk_images(doc, images, newtags)
        for ref in images:
            reason = _ref_is_floating(ref)
            if reason:
                errors.append(f"{path.name}: image '{ref}' {reason}")
        for tag in newtags:
            if tag == "latest":
                errors.append(f"{path.name}: newTag 'latest' is not allowed")
    return errors


# ---- network policies ---------------------------------------------------------


def _selects_datastore(selector: dict[str, Any] | None) -> bool:
    """True if a podSelector COULD match a datastore pod — conservative, so an
    over-broad or matchExpressions-based selector can't sneak a datastore past the check."""
    if not selector:  # None or {} matches ALL pods, datastores included
        return True
    labels = selector.get("matchLabels") or {}
    if "cnpg.io/cluster" in labels:
        return True
    comp = labels.get("app.kubernetes.io/component")
    if comp in _DATASTORE_COMPONENTS:
        return True
    if comp is not None:
        return False  # pins a non-datastore component → excludes datastores
    for expr in selector.get("matchExpressions") or []:
        if expr.get("key") != "app.kubernetes.io/component":
            continue
        op, vals = expr.get("operator"), set(expr.get("values") or [])
        if op == "In":
            return bool(vals & _DATASTORE_COMPONENTS)  # In non-datastore ⇒ excludes
        if op == "NotIn":
            return not vals.issuperset(_DATASTORE_COMPONENTS)  # unless it excludes ALL datastores
        if op in ("Exists", "DoesNotExist"):
            return True
    # a selector that doesn't pin the component (e.g. only part-of: titan) matches datastores
    return True


def networkpolicy_errors(files: list[Path]) -> list[str]:
    errors: list[str] = []
    policies = [d for d in _docs(files) if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"]

    # 1. a TRUE default-deny-ingress must exist: empty podSelector, Ingress, and NO rules
    #    (an allow-all `podSelector:{} + ingress:[{}]` must NOT count as the deny).
    default_deny = any(
        (p.get("spec") or {}).get("podSelector") == {}
        and "Ingress" in ((p.get("spec") or {}).get("policyTypes") or [])
        and not (p.get("spec") or {}).get("ingress")
        for p in policies
    )
    if not default_deny:
        errors.append("no true default-deny-ingress policy (empty podSelector, Ingress, no rules)")

    # 2. no datastore-selecting policy may admit ingress from a namespace, an ipBlock, or
    #    all sources (empty `from`).
    for p in policies:
        spec = p.get("spec") or {}
        if "Ingress" not in (spec.get("policyTypes") or []):
            continue
        if not _selects_datastore(spec.get("podSelector")):
            continue
        name = (p.get("metadata") or {}).get("name", "<no-name>")
        for rule in spec.get("ingress") or []:
            peers = rule.get("from")
            if not peers:
                errors.append(f"{name}: datastore ingress allows ALL sources (empty from, OPS-007)")
                continue
            for peer in peers:
                if "namespaceSelector" in peer:
                    errors.append(f"{name}: datastore ingress from a namespaceSelector — OPS-007")
                if "ipBlock" in peer:
                    errors.append(f"{name}: datastore ingress from an ipBlock — OPS-007")
    return errors


def collect_errors(root: Path) -> list[str]:
    files = _yaml_files(root)
    return image_tag_errors(files) + networkpolicy_errors(files)


def main() -> None:
    if not K8S_DIR.is_dir():
        raise SystemExit(f"check-k8s: no manifests under {K8S_DIR}")
    errors = collect_errors(K8S_DIR)
    if errors:
        sys.stderr.write("k8s-manifest check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write("k8s-manifest check OK: no floating images; datastores edge-unreachable.\n")


if __name__ == "__main__":
    main()
