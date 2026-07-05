"""Phase 12 integration tests: agent registry CRUD + :pause/:resume.

Happy path + auth failure + validation failure per endpoint, plus the
:pause/:resume state machine and its 409s. Needs the disposable test DB
(schema marker) + fakeredis."""

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.schema

_MODEL_BINDING = {"provider": "ollama", "model": "qwen2.5-coder:14b", "temperature": 0.2}


def _new_agent(api: dict[str, Any], *, role: str = "backend", token: str = "owner") -> Any:
    resp = api["client"].post(
        "/api/v1/agents",
        json={"role": role, "name": f"{role}-bot", "model_binding": _MODEL_BINDING},
        headers=api["token_for"](token),
    )
    assert resp.status_code == 201, resp.text
    return resp


def test_create_agent_201(api: dict[str, Any]) -> None:
    resp = _new_agent(api, role="architect")
    body = resp.json()
    assert uuid.UUID(body["id"]) and body["role"] == "architect"
    assert body["status"] == "idle" and body["version"] == 1
    assert body["model_binding"] == _MODEL_BINDING and body["config"] == {}
    assert resp.headers["etag"] == '"v1"'


def test_create_agent_requires_configure_permission(api: dict[str, Any]) -> None:
    # maintainer lacks agent:configure (only owner/admin have it).
    resp = api["client"].post(
        "/api/v1/agents",
        json={"role": "backend", "name": "x", "model_binding": _MODEL_BINDING},
        headers=api["token_for"]("maintainer"),
    )
    assert resp.status_code == 403


def test_create_agent_unauthenticated_401(api: dict[str, Any]) -> None:
    resp = api["client"].post(
        "/api/v1/agents", json={"role": "backend", "name": "x", "model_binding": {}}
    )
    assert resp.status_code == 401


def test_create_agent_invalid_role_422(api: dict[str, Any]) -> None:
    resp = api["client"].post(
        "/api/v1/agents",
        json={"role": "overlord", "name": "x", "model_binding": _MODEL_BINDING},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422
    assert resp.json()["type"] == "https://titan.dev/errors/validation"


def test_create_agent_unknown_project_422(api: dict[str, Any]) -> None:
    """A well-formed but non-existent project_id is a 422, not a 500 (review)."""
    resp = api["client"].post(
        "/api/v1/agents",
        json={
            "role": "backend",
            "name": "x",
            "model_binding": _MODEL_BINDING,
            "project_id": str(uuid.uuid4()),
        },
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422
    assert resp.json()["type"] == "https://titan.dev/errors/validation"


def test_get_and_list_agents(api: dict[str, Any]) -> None:
    aid = _new_agent(api, role="qa").json()["id"]
    client, headers = api["client"], api["token_for"]("viewer")  # *:read can read

    got = client.get(f"/api/v1/agents/{aid}", headers=headers)
    assert got.status_code == 200 and got.headers["etag"] == '"v1"'

    listed = client.get("/api/v1/agents?role=qa&limit=100", headers=headers)
    assert listed.status_code == 200
    assert aid in {a["id"] for a in listed.json()["items"]}
    assert all(a["role"] == "qa" for a in listed.json()["items"])


def test_get_agent_404(api: dict[str, Any]) -> None:
    resp = api["client"].get(f"/api/v1/agents/{uuid.uuid4()}", headers=api["token_for"]("owner"))
    assert resp.status_code == 404


def test_patch_agent_if_match(api: dict[str, Any]) -> None:
    aid = _new_agent(api).json()["id"]
    client, headers = api["client"], api["token_for"]("owner")
    resp = client.patch(
        f"/api/v1/agents/{aid}",
        json={"name": "renamed", "config": {"max_parallel": 2}},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed" and resp.json()["config"] == {"max_parallel": 2}
    assert resp.json()["version"] == 2


def test_patch_agent_rejects_status_field_422(api: dict[str, Any]) -> None:
    # status moves via :pause/:resume, never PATCH (extra="forbid").
    aid = _new_agent(api).json()["id"]
    resp = api["client"].patch(
        f"/api/v1/agents/{aid}",
        json={"status": "busy"},
        headers={**api["token_for"]("owner"), "If-Match": '"v1"'},
    )
    assert resp.status_code == 422


def test_patch_agent_stale_etag_412(api: dict[str, Any]) -> None:
    aid = _new_agent(api).json()["id"]
    client, headers = api["client"], api["token_for"]("owner")
    client.patch(
        f"/api/v1/agents/{aid}", json={"name": "a"}, headers={**headers, "If-Match": '"v1"'}
    )
    stale = client.patch(
        f"/api/v1/agents/{aid}", json={"name": "b"}, headers={**headers, "If-Match": '"v1"'}
    )
    assert stale.status_code == 412


def test_pause_resume_state_machine(api: dict[str, Any]) -> None:
    aid = _new_agent(api).json()["id"]
    client, headers = api["client"], api["token_for"]("owner")

    paused = client.post(f"/api/v1/agents/{aid}:pause", headers=headers)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"

    # Pausing an already-paused agent → 409.
    again = client.post(f"/api/v1/agents/{aid}:pause", headers=headers)
    assert again.status_code == 409
    assert again.json()["type"] == "https://titan.dev/errors/conflict"

    resumed = client.post(f"/api/v1/agents/{aid}:resume", headers=headers)
    assert resumed.status_code == 200 and resumed.json()["status"] == "idle"

    # Resuming a non-paused agent → 409 (the phase's named example).
    bad = client.post(f"/api/v1/agents/{aid}:resume", headers=headers)
    assert bad.status_code == 409


def test_pause_missing_agent_404(api: dict[str, Any]) -> None:
    resp = api["client"].post(
        f"/api/v1/agents/{uuid.uuid4()}:pause", headers=api["token_for"]("owner")
    )
    assert resp.status_code == 404


def test_pause_requires_configure(api: dict[str, Any]) -> None:
    aid = _new_agent(api).json()["id"]
    resp = api["client"].post(f"/api/v1/agents/{aid}:pause", headers=api["token_for"]("viewer"))
    assert resp.status_code == 403
