"""CI gate for the blue/green topology (Phase 80 part 2, Vol 9 §5.1, TITAN-OPS-010/011).

kubeconform validates each object's schema; this validates the cross-object consistency that
silently breaks a blue/green flip if it drifts — the class of latent error the Phase-80-part-1
review flagged (F4) and the "manifest-error-prone" lesson warns about:

1. **Every stateless Deployment is color-partitioned.** `<svc>-blue`/`<svc>-green`, and the color
   in the name MUST equal `spec.selector.matchLabels.color` AND `spec.template.metadata.labels
   .color`. The color MUST be in the selector, or two colors' Deployments would own the same
   pods (the selector is what makes blue and green disjoint).
2. **The stable Service pins the active color.** Each HTTP tier's stable Service selector carries
   a `color` that a committed Deployment actually has (else it selects zero pods = an outage on
   apply). The worker's Service is color-AGNOSTIC on purpose (pull-based; flip is scale-based).
3. **A preview Service exists per color** for each HTTP tier (`<svc>-blue`/`<svc>-green`), each
   pinned to its own color — the release job smoke-tests the idle color through these before the
   flip; a missing/mis-pinned preview means the smoke step tests the wrong (or no) pods.
4. **Every autoscaler resolves + is color-labelled (the F4 fix).** An HPA/ScaledObject
   `scaleTargetRef` MUST name a Deployment that exists (a bare `api` ref never resolves once the
   Deployments are `api-blue`), and it MUST carry a `color` label matching its target's color so
   `release.sh` can drop the retired color's autoscaler by label BEFORE scaling it to zero.
5. **Only the active (blue) color's autoscalers are committed.** A committed green autoscaler would
   re-inflate the idle color on `apply -k` (exactly the F4 collision). Green autoscalers are
   rendered/dropped by release.sh at flip time, never stored in git.

Pure filesystem + YAML — no cluster. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "base"

STATELESS_COMPONENTS = {"frontend", "api", "orchestrator", "worker", "llm-gateway"}
# HTTP tiers get a stable + per-color preview Service and a color-pinned stable selector.
# The worker is pull-based (headless, color-agnostic Service; flip is scale-based) — excluded.
HTTP_TIERS = {"frontend", "api", "orchestrator", "llm-gateway"}
COLORS = ("blue", "green")
ACTIVE_COLOR = "blue"  # the git-canonical live color; green is script-managed

_COLOR_SUFFIX = re.compile(r"-(blue|green)$")


def _color_of(name: str) -> str | None:
    m = _COLOR_SUFFIX.search(name)
    return m.group(1) if m else None


def bluegreen_errors(docs: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    services = [d for d in docs if d.get("kind") == "Service"]
    autoscalers = [d for d in docs if d.get("kind") in ("HorizontalPodAutoscaler", "ScaledObject")]

    deploy_names: set[str] = set()
    present_colors: dict[str, set[str]] = defaultdict(set)  # component -> committed colors

    # 1. Deployments are color-partitioned and self-consistent.
    for d in deployments:
        meta = d.get("metadata") or {}
        name = meta.get("name", "<no-name>")
        deploy_names.add(name)
        spec = d.get("spec") or {}
        sel = ((spec.get("selector") or {}).get("matchLabels")) or {}
        tpl = (((spec.get("template") or {}).get("metadata") or {}).get("labels")) or {}
        # Identify the tier from ANY label source (authoritative: metadata.labels) so a
        # Deployment cannot dodge the color checks by using a matchExpressions selector or by
        # leaving the component label out of selector.matchLabels.
        comp = (
            (meta.get("labels") or {}).get("app.kubernetes.io/component")
            or sel.get("app.kubernetes.io/component")
            or tpl.get("app.kubernetes.io/component")
        )
        if comp not in STATELESS_COMPONENTS:
            continue  # datastores (postgres/redis/qdrant) are shared across colors — never split
        name_color = _color_of(name)
        if name_color is None:
            errors.append(
                f"Deployment {name}: stateless tier must be color-suffixed <svc>-blue/green"
            )
            continue
        sel_color = sel.get("color")
        tpl_color = tpl.get("color")
        if sel_color is None:
            errors.append(
                f"Deployment {name}: 'color' missing from selector.matchLabels "
                f"(blue and green would own the same pods)"
            )
        elif sel_color != name_color:
            errors.append(f"Deployment {name}: selector color '{sel_color}' != name '{name_color}'")
        if tpl_color != name_color:
            errors.append(
                f"Deployment {name}: pod-template color '{tpl_color}' != name '{name_color}'"
            )
        present_colors[comp].add(name_color)

    # index Service selectors by Service name
    svc_selector: dict[str, dict[str, Any]] = {}
    for s in services:
        sname = (s.get("metadata") or {}).get("name", "<no-name>")
        svc_selector[sname] = (s.get("spec") or {}).get("selector") or {}

    # 2 + 3. stable Service pins an existing color; a preview Service exists per color.
    for comp in sorted(HTTP_TIERS):
        stable = svc_selector.get(comp)
        if stable is None:
            errors.append(f"Service {comp}: stable Service is missing")
        else:
            color = stable.get("color")
            if color is None:
                errors.append(
                    f"Service {comp}: stable selector must pin a 'color' (blue/green flip)"
                )
            elif color not in present_colors.get(comp, set()):
                errors.append(
                    f"Service {comp}: no {comp}-{color} Deployment exists for selected color"
                )
        for color in COLORS:
            preview = f"{comp}-{color}"
            psel = svc_selector.get(preview)
            if psel is None:
                errors.append(f"Service {preview}: preview Service (idle-color smoke) is missing")
                continue
            if psel.get("color") != color:
                got = psel.get("color")
                errors.append(f"Service {preview}: preview must pin color '{color}', got '{got}'")
            if psel.get("app.kubernetes.io/component") != comp:
                errors.append(f"Service {preview}: preview must select component '{comp}'")

    # worker Service must stay color-agnostic (pull-based; no Service-selector flip)
    wsel = svc_selector.get("worker")
    if wsel is not None and wsel.get("color") is not None:
        errors.append("Service worker: must be color-AGNOSTIC (pull-based; flip is scale-based)")

    # 4 + 5. autoscalers resolve, are color-labelled, and only the active color is committed.
    for a in autoscalers:
        meta = a.get("metadata") or {}
        name = meta.get("name", "<no-name>")
        kind = a.get("kind")
        target = ((a.get("spec") or {}).get("scaleTargetRef") or {}).get("name", "")
        if target not in deploy_names:
            errors.append(
                f"{kind} {name}: scaleTargetRef '{target}' matches no Deployment "
                f"(a bare/stale ref never resolves — the F4 collision)"
            )
        # 1:1 naming: the autoscaler is named exactly for the Deployment it scales, so a
        # mis-targeted autoscaler (e.g. api-blue scaling frontend-blue under api's rules) is
        # caught, and release.sh's per-color render/retire stays unambiguous.
        if target and name != target:
            errors.append(
                f"{kind} {name}: name must equal scaleTargetRef '{target}' (1:1 per color)"
            )
        acolor = (meta.get("labels") or {}).get("color")
        tcolor = _color_of(target)
        if acolor is None:
            errors.append(
                f"{kind} {name}: missing 'color' label (release.sh drops the retired one by color)"
            )
        elif tcolor is not None and acolor != tcolor:
            errors.append(f"{kind} {name}: color label '{acolor}' != target color '{tcolor}'")
        if acolor is not None and acolor != ACTIVE_COLOR:
            errors.append(
                f"{kind} {name}: only the active ({ACTIVE_COLOR}) autoscaler may be committed; a "
                f"'{acolor}' one would re-inflate an idle color on apply (F4)"
            )
    return errors


def _docs_from_dir(base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted([*base.rglob("*.yaml"), *base.rglob("*.yml")]):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict):
                out.append(doc)
    return out


def main() -> None:
    if not BASE_DIR.is_dir():
        raise SystemExit(f"check-bluegreen: no manifests under {BASE_DIR}")
    errors = bluegreen_errors(_docs_from_dir(BASE_DIR))
    if errors:
        sys.stderr.write("blue/green check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        "blue/green OK: colors partitioned, stable/preview Services consistent, "
        "autoscalers resolve + are color-scoped.\n"
    )


if __name__ == "__main__":
    main()
