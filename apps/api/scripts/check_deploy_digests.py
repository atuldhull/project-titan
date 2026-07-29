"""CI gate: deploy manifests reference @sha256: digests only (Phase 76, OPS-005).

Vol 9 §3.1: "deploy manifests MUST pin digests, never floating tags." This scans the
deploy manifests under `infra/deploy/` and fails if any TITAN app image
(ghcr.io/titan-platform/*) is referenced by a floating tag instead of an immutable
`@sha256:` digest, or if ANY image uses `:latest`. It is a forward-guard: with no
deploy manifests yet it passes, and it turns red the moment a floating-tag manifest
is committed. The build-time digests come from main.yml's cosign-signed push.

Pure filesystem — no DB, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[3] / "infra" / "deploy"
_APP_REGISTRY = "ghcr.io/titan-platform/"
# A TITAN app image reference ANYWHERE in the manifest (image:/args/command/Helm
# templates), not just an `image:` key — so a floating ref can't hide off the image
# line (review #12). Captures an optional :tag and optional @sha256: digest.
_APP_REF = re.compile(r"ghcr\.io/titan-platform/[\w.\-]+(?::[\w.\-]+)?(?:@sha256:[0-9a-f]+)?")
# kustomize splits the ref into `name:` + `newTag:`; a floating newTag is a floating pin.
_NEWTAG = re.compile(r"""(?m)^\s*newTag:\s*["']?([^\s"'#]+)""")


def collect_errors(sources: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for name, text in sorted(sources.items()):
        for ref in _APP_REF.findall(text):
            if ":latest" in ref:
                errors.append(f"{name}: image '{ref}' uses :latest — pin an immutable digest")
            elif "@sha256:" not in ref and ":" in ref[len(_APP_REGISTRY) :]:
                # has a tag but no digest → floating
                errors.append(
                    f"{name}: TITAN image '{ref}' is not digest-pinned — deploy manifests must "
                    "reference @sha256: only (OPS-005), never a floating tag"
                )
        for tag in _NEWTAG.findall(text):
            if not tag.startswith("sha256:"):
                errors.append(
                    f"{name}: kustomize newTag '{tag}' is a floating tag — pin @sha256 (OPS-005)"
                )
    return errors


def main() -> None:
    sources: dict[str, str] = {}
    if DEPLOY_DIR.exists():
        for path in DEPLOY_DIR.rglob("*"):
            if path.suffix.lower() in {".yaml", ".yml"} and path.is_file():
                sources[str(path.relative_to(DEPLOY_DIR.parent))] = path.read_text(
                    encoding="utf-8", errors="ignore"
                )
    errors = collect_errors(sources)
    if errors:
        sys.stderr.write("deploy-digest check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        f"deploy-digest OK: {len(sources)} manifest(s), all TITAN images digest-pinned.\n"
    )


if __name__ == "__main__":
    main()
