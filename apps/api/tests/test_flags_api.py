"""Phase 72 (Vol 9 §5.3): the /api/v1/flags admin + read API — CRUD, the
flag:read (rides *:read) vs flag:write (Owner/Admin) split, and the ops
kill-switch delete guard."""

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.schema


def _key() -> str:
    return f"api-flag-{uuid.uuid4().hex[:10]}"


def _create_body(key: str, **kw: Any) -> dict[str, Any]:
    body = {"key": key, "flag_type": "boolean", "flag_class": "release", "owner": "ops"}
    body.update(kw)
    return body


def test_any_authenticated_role_can_read_flags(api: dict[str, Any]) -> None:
    # flag:read is NON-sensitive, so it rides *:read: viewers AND agents (operator)
    # can list, which is what the dashboard + orchestrator context need.
    for role in ("viewer", "operator", "admin", "owner"):
        r = api["client"].get("/api/v1/flags", headers=api["token_for"](role))
        assert r.status_code == 200, (role, r.text)
    keys = {
        f["key"]
        for f in api["client"].get("/api/v1/flags", headers=api["token_for"]("viewer")).json()
    }
    assert {"agents.pause_all", "intake.read_only", "llm.provider_override"} <= keys


def test_get_one_and_404(api: dict[str, Any]) -> None:
    ok = api["client"].get("/api/v1/flags/agents.pause_all", headers=api["token_for"]("viewer"))
    assert ok.status_code == 200 and ok.json()["flag_class"] == "ops"
    missing = api["client"].get(f"/api/v1/flags/{_key()}", headers=api["token_for"]("viewer"))
    assert missing.status_code == 404


def test_only_admins_write(api: dict[str, Any]) -> None:
    key = _key()
    body = _create_body(key)
    # viewer, operator (agent), maintainer all lack flag:write.
    for role in ("viewer", "operator", "maintainer"):
        r = api["client"].post("/api/v1/flags", json=body, headers=api["token_for"](role))
        assert r.status_code == 403, (role, r.text)
    # owner + admin can.
    created = api["client"].post("/api/v1/flags", json=body, headers=api["token_for"]("admin"))
    assert created.status_code == 201, created.text
    assert created.json()["key"] == key and created.json()["enabled"] is False


def test_create_conflict_is_409(api: dict[str, Any]) -> None:
    key = _key()
    body = _create_body(key)
    assert (
        api["client"]
        .post("/api/v1/flags", json=body, headers=api["token_for"]("owner"))
        .status_code
        == 201
    )
    dup = api["client"].post("/api/v1/flags", json=body, headers=api["token_for"]("owner"))
    assert dup.status_code == 409


def test_flip_a_kill_switch_via_the_api(api: dict[str, Any]) -> None:
    """The TITAN-OPS-012 control surface: an operator flips agents.pause_all via
    the API. PUT is partial — only `enabled` changes."""
    before = (
        api["client"]
        .get("/api/v1/flags/agents.pause_all", headers=api["token_for"]("owner"))
        .json()
    )
    assert before["enabled"] is False
    flipped = api["client"].put(
        "/api/v1/flags/agents.pause_all",
        json={"enabled": True},
        headers=api["token_for"]("owner"),
    )
    assert flipped.status_code == 200 and flipped.json()["enabled"] is True
    assert flipped.json()["flag_class"] == "ops"  # untouched by the partial update
    # A viewer cannot flip it back.
    assert (
        api["client"]
        .put(
            "/api/v1/flags/agents.pause_all",
            json={"enabled": False},
            headers=api["token_for"]("viewer"),
        )
        .status_code
        == 403
    )
    # Reset so the shared-DB session is left as seeded.
    api["client"].put(
        "/api/v1/flags/agents.pause_all",
        json={"enabled": False},
        headers=api["token_for"]("owner"),
    )


def test_update_missing_is_404(api: dict[str, Any]) -> None:
    r = api["client"].put(
        f"/api/v1/flags/{_key()}", json={"enabled": True}, headers=api["token_for"]("owner")
    )
    assert r.status_code == 404


def test_delete_rules(api: dict[str, Any]) -> None:
    # An ops kill switch is permanent — cannot be deleted (409).
    protected = api["client"].delete(
        "/api/v1/flags/intake.read_only", headers=api["token_for"]("owner")
    )
    assert protected.status_code == 409
    # A release flag deletes (204), then 404 on the second try.
    key = _key()
    api["client"].post("/api/v1/flags", json=_create_body(key), headers=api["token_for"]("owner"))
    assert (
        api["client"].delete(f"/api/v1/flags/{key}", headers=api["token_for"]("owner")).status_code
        == 204
    )
    assert (
        api["client"].delete(f"/api/v1/flags/{key}", headers=api["token_for"]("owner")).status_code
        == 404
    )
    # A viewer cannot delete.
    api["client"].post(
        "/api/v1/flags", json=_create_body(_key()), headers=api["token_for"]("owner")
    )
    assert (
        api["client"]
        .delete("/api/v1/flags/agents.pause_all", headers=api["token_for"]("viewer"))
        .status_code
        == 403
    )


def test_percentage_flag_requires_a_percentage(api: dict[str, Any]) -> None:
    # The FlagCreate model validator rejects a percentage flag with no percentage.
    r = api["client"].post(
        "/api/v1/flags",
        json=_create_body(_key(), flag_type="percentage"),
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 422
