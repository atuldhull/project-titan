"""Phase 76 (Vol 9 §3.1, OPS-005): deploy manifests must pin @sha256: digests."""

from __future__ import annotations

from typing import Any

from scripts.check_deploy_digests import collect_errors

pytestmark: list[Any] = []

_DIGEST = "ghcr.io/titan-platform/titan-api@sha256:" + "a" * 64


def test_floating_titan_tag_is_flagged() -> None:
    errors = collect_errors(
        {"staging.yaml": "  image: ghcr.io/titan-platform/titan-api:sha-abc123\n"}
    )
    assert any("digest-pinned" in e for e in errors), errors


def test_latest_is_flagged() -> None:
    errors = collect_errors({"m.yaml": "image: ghcr.io/titan-platform/titan-api:latest\n"})
    assert any(":latest" in e for e in errors), errors


def test_digest_pinned_titan_image_is_ok() -> None:
    assert collect_errors({"m.yaml": f"image: {_DIGEST}\n"}) == []


def test_third_party_pinned_tag_is_ok() -> None:
    # Non-TITAN infra images with a specific tag (not :latest) are acceptable.
    assert collect_errors({"m.yaml": "image: postgres:16-alpine\n"}) == []


def test_kustomize_floating_newtag_is_flagged() -> None:
    manifest = "images:\n  - name: ghcr.io/titan-platform/titan-api\n    newTag: sha-abc123\n"
    assert any("newTag" in e for e in collect_errors({"k.yaml": manifest})), "should flag newTag"


def test_app_ref_off_the_image_line_is_flagged() -> None:
    # a floating app ref hiding in args, not on an image: key
    manifest = "  args: ['--sidecar', 'ghcr.io/titan-platform/titan-worker:sha-xyz']\n"
    assert any("digest-pinned" in e for e in collect_errors({"m.yaml": manifest}))
