"""The evolution API (Phase 83, Vol 10 §2/§6.1).

End-to-end over the real app + schema: read endpoints ride evolution:read
(*:read), and the approve action requires evolution:approve (Owner/Admin/
Maintainer, human-only) and lands in the audit log (TITAN-EVO-001c).
"""

from typing import Any

import pytest

pytestmark = pytest.mark.schema

_REVIEWER_PATH = "evolution/prompts/reviewer/system.md"


def test_manifest_and_prompts_are_readable_by_any_role(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    m = client.get("/api/v1/evolution/manifest", headers=token_for("viewer"))
    assert m.status_code == 200
    # 16 core roster prompts + localization sample + 2 CEO-intake prompts.
    assert m.json()["prompt_count"] == 19

    p = client.get("/api/v1/evolution/prompts", headers=token_for("viewer"))
    assert p.status_code == 200
    prompts = p.json()["prompts"]
    assert "reviewer" in prompts and len(prompts["reviewer"]["sha256"]) == 64


def test_artifact_ledger_populated_from_manifest_at_startup(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    r = client.get("/api/v1/evolution/artifacts", headers=token_for("admin"))
    assert r.status_code == 200
    refs = {a["ref"] for a in r.json()["artifacts"]}
    assert {"reviewer", "base", "routing"} <= refs


def test_reader_cannot_approve(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    sha = client.get("/api/v1/evolution/prompts", headers=token_for("viewer")).json()["prompts"][
        "reviewer"
    ]["sha256"]
    body = {
        "kind": "prompt",
        "ref": "reviewer",
        "path": _REVIEWER_PATH,
        "content_sha256": sha,
        "semver": "1.0.0",
        "ep": "EP-0001",
    }
    r = client.post("/api/v1/evolution/artifacts:approve", json=body, headers=token_for("viewer"))
    assert r.status_code == 403


def test_admin_approves_and_it_lands_in_the_audit_log(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    sha = client.get("/api/v1/evolution/prompts", headers=token_for("admin")).json()["prompts"][
        "reviewer"
    ]["sha256"]
    body = {
        "kind": "prompt",
        "ref": "reviewer",
        "path": _REVIEWER_PATH,
        "content_sha256": sha,
        "semver": "1.0.0",
        "ep": "EP-0042",
        "diff_url": "https://example.test/pr/42",
    }
    r = client.post("/api/v1/evolution/artifacts:approve", json=body, headers=token_for("admin"))
    assert r.status_code == 201, r.text
    assert r.json()["approved"] is True
    assert r.json()["artifact"]["approved_by"]

    # EVO-001c: the approval is searchable in the audit log with the artifact hash.
    events = client.get(
        "/api/v1/audit", params={"action": "evolution.approve"}, headers=token_for("admin")
    )
    assert events.status_code == 200
    hits = [e for e in events.json() if e["resource"] == _REVIEWER_PATH]
    assert hits and hits[0]["detail"]["artifact_hash"] == sha


def test_approve_rejects_a_phantom_hash(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    body = {
        "kind": "prompt",
        "ref": "reviewer",
        "path": _REVIEWER_PATH,
        "content_sha256": "0" * 64,  # valid shape, wrong bytes
        "semver": "1.0.0",
        "ep": "EP-0001",
    }
    r = client.post("/api/v1/evolution/artifacts:approve", json=body, headers=token_for("admin"))
    assert r.status_code == 400
