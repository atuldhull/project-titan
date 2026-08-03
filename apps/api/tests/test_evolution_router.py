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
    # 16 core roster prompts + localization sample + 2 CEO-intake + scorecard rubric
    # + 3 Phase-85 registry prompts (failure_classifier, post_task_reflection, retrospective).
    assert m.json()["prompt_count"] == 23

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


def test_agent_kpis_endpoint(api: dict[str, Any]) -> None:
    import uuid
    from datetime import UTC, datetime

    from tests.conftest import run_async
    from tests.test_evolution_scorecard import _seed_project_task, _seed_scorecard

    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    agent = f"kpiagent_{uuid.uuid4().hex[:8]}"

    async def seed() -> None:
        pid, _ = await _seed_project_task(engine, with_run=False)
        for c in (70.0, 90.0):
            await _seed_scorecard(
                engine,
                pid,
                agent=agent,
                task_class="code",
                composite=c,
                created_at=datetime.now(UTC),
            )

    run_async(seed)
    r = client.get(f"/api/v1/evolution/agents/{agent}/kpis", headers=token_for("viewer"))
    assert r.status_code == 200
    windows = r.json()["windows"]
    assert windows["7d"]["code"]["count"] == 2
    assert abs(windows["7d"]["code"]["composite"] - 80.0) < 0.5  # reconciles with the raw mean


def test_investigations_endpoint(api: dict[str, Any]) -> None:
    import uuid

    from tests.conftest import run_async
    from titan.evolution import repository as repo

    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    agent = f"invagent_{uuid.uuid4().hex[:8]}"

    async def seed() -> None:
        await repo.open_investigation(
            engine,
            agent_id=agent,
            task_class="code",
            window_days=3,
            recent_value=45.0,
            baseline_value=90.0,
            drop_pct=50.0,
            series_snapshot={"recent": {"count": 3}},
        )

    run_async(seed)
    r = client.get("/api/v1/evolution/investigations", headers=token_for("viewer"))
    assert r.status_code == 200
    mine = [i for i in r.json()["investigations"] if i["agent_id"] == agent]
    assert (
        mine and mine[0]["drop_pct"] == 50.0 and mine[0]["series_snapshot"]["recent"]["count"] == 3
    )


def test_rcrs_list_and_human_only_ep_link(api: dict[str, Any]) -> None:
    import uuid

    from tests.conftest import run_async
    from titan.evolution import failures

    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    sig = f"F-SEC:agent.security:{uuid.uuid4().hex[:8]}"

    async def seed() -> str:
        created = await failures.open_or_reopen_rcr(
            engine,
            failure_code="F-SEC",
            severity="high",
            signature=sig,
            summary="router-test RCR",
        )
        assert created is not None
        return created["id"]

    rcr_id = run_async(seed)

    # Listable by any reader.
    r = client.get("/api/v1/evolution/rcrs", headers=token_for("viewer"))
    assert r.status_code == 200
    assert any(x["id"] == rcr_id for x in r.json()["rcrs"])

    # A reader cannot link an EP (lacks evolution:approve).
    denied = client.patch(
        f"/api/v1/evolution/rcrs/{rcr_id}/ep", json={"ep": "EP-9"}, headers=token_for("viewer")
    )
    assert denied.status_code == 403

    # An admin (human) can link the remediating EP.
    ok = client.patch(
        f"/api/v1/evolution/rcrs/{rcr_id}/ep", json={"ep": "EP-77"}, headers=token_for("admin")
    )
    assert ok.status_code == 200 and ok.json()["rcr"]["ep"] == "EP-77"

    # An unknown RCR is a clean 404.
    missing = client.patch(
        f"/api/v1/evolution/rcrs/{uuid.uuid4()}/ep", json={"ep": "EP-1"}, headers=token_for("admin")
    )
    assert missing.status_code == 404

    # The link is audited (trust-relevant curation; mirrors approve).
    events = client.get(
        "/api/v1/audit", params={"action": "evolution.rcr_update"}, headers=token_for("admin")
    )
    assert events.status_code == 200
    hits = [e for e in events.json() if e["resource"] == f"rcr:{rcr_id}"]
    assert hits and hits[0]["detail"]["op"] == "link_ep"


def test_rcr_override_is_human_only_and_taxonomy_validated(api: dict[str, Any]) -> None:
    import uuid

    from tests.conftest import run_async
    from titan.evolution import failures

    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    sig = f"F-GEN:agent.eng_frontend:{uuid.uuid4().hex[:8]}"

    async def seed() -> str:
        created = await failures.open_or_reopen_rcr(
            engine, failure_code="F-GEN", severity="low", signature=sig, summary="override-test"
        )
        assert created is not None
        return created["id"]

    rcr_id = run_async(seed)

    # A reader cannot override.
    denied = client.patch(
        f"/api/v1/evolution/rcrs/{rcr_id}",
        json={"severity": "high"},
        headers=token_for("viewer"),
    )
    assert denied.status_code == 403

    # An out-of-taxonomy code is a clean 400 (not a DB 500).
    bad = client.patch(
        f"/api/v1/evolution/rcrs/{rcr_id}",
        json={"failure_code": "F-BOGUS"},
        headers=token_for("admin"),
    )
    assert bad.status_code == 400

    # An admin overrides classification + status.
    ok = client.patch(
        f"/api/v1/evolution/rcrs/{rcr_id}",
        json={"failure_code": "F-SEC", "status": "accepted_risk"},
        headers=token_for("admin"),
    )
    assert ok.status_code == 200
    assert ok.json()["rcr"]["failure_code"] == "F-SEC"
    assert ok.json()["rcr"]["status"] == "accepted_risk"


def test_merged_ep_remediates_its_linked_rcr(api: dict[str, Any]) -> None:
    """EVO-013 end-to-end: an approved artifact carrying an EP auto-remediates the
    RCRs linked to that EP (via service.approve_artifact → annotate)."""
    import uuid

    from tests.conftest import run_async
    from titan.evolution import failures
    from titan.evolution import repository as repo

    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    ep = f"EP-REMED-{uuid.uuid4().hex[:8]}"
    sig = f"F-STD:agent.eng_backend:{uuid.uuid4().hex[:8]}"

    async def seed() -> str:
        created = await failures.open_or_reopen_rcr(
            engine, failure_code="F-STD", severity="high", signature=sig, summary="lint drift"
        )
        assert created is not None
        linked = await repo.link_rcr_ep(engine, uuid.UUID(created["id"]), ep)
        assert linked is not None and linked["status"] == "open"
        return created["id"]

    rcr_id = run_async(seed)

    sha = client.get("/api/v1/evolution/prompts", headers=token_for("admin")).json()["prompts"][
        "reviewer"
    ]["sha256"]
    body = {
        "kind": "prompt",
        "ref": "reviewer",
        "path": _REVIEWER_PATH,
        "content_sha256": sha,
        "semver": "1.0.0",
        "ep": ep,
    }
    r = client.post("/api/v1/evolution/artifacts:approve", json=body, headers=token_for("admin"))
    assert r.status_code == 201, r.text

    async def fetch() -> dict[str, Any]:
        rows = await repo.list_rcrs(engine)
        return next(x for x in rows if x["id"] == rcr_id)

    rcr = run_async(fetch)
    assert rcr["ep_merged"] is True and rcr["status"] == "remediated"


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
