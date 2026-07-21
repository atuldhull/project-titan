"""The write-only secrets API (Phase 68, TITAN-SEC-051). Full app over the test
DB. Proves write-only semantics (405/404 on any read-back), the RBAC carve-out
(secret:read/write are NOT reached by *:read), and the audit trail."""

from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.vault import age

pytestmark = pytest.mark.schema


@pytest.fixture
def vault_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a writable age vault + deterministic field key BEFORE the app
    builds. Listed before ``api`` in each test signature so its env is set first."""
    identity, _ = age.generate_identity()
    monkeypatch.setenv("TITAN_VAULT_AGE_KEY", identity)
    monkeypatch.setenv("TITAN_VAULT_AGE_FILE", str(tmp_path / "secrets.yaml.age"))
    monkeypatch.setenv("TITAN_FIELD_ENCRYPTION_KEY", "test-field-key")
    monkeypatch.setenv("TITAN_BUS_HMAC_KEY", "test-bus-key")
    return identity


def _create(client: Any, token: dict[str, str], **body: Any) -> Any:
    payload = {"name": "GITHUB_TOKEN", "value": "ghp_secret", "rotation_days": 90}
    payload.update(body)
    return client.post("/api/v1/secrets", json=payload, headers=token)


def test_create_returns_metadata_never_value(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    resp = _create(client, token)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "GITHUB_TOKEN"
    assert body["overdue"] is False
    assert "value" not in body  # SEC-051: a value never round-trips
    # …and the value is genuinely in the vault (store), just never via the API.
    engine = api["engine"]

    async def audited() -> int:
        async with engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM secret_audit "
                            "WHERE event_type = 'secret.created' AND secret_name = 'GITHUB_TOKEN'"
                        )
                    )
                ).scalar_one()
            )

    assert run_async(audited) == 1  # §10.2 lifecycle audit, no value


def test_value_read_is_405(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    _create(client, token)
    resp = client.get("/api/v1/secrets/GITHUB_TOKEN/value", headers=token)
    assert resp.status_code == 405  # write-only; never a value
    assert resp.headers.get("content-type", "").startswith("application/problem+json")


def test_metadata_read_never_includes_value(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    _create(client, token)
    listed = client.get("/api/v1/secrets", headers=token)
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1 and "value" not in items[0]
    one = client.get("/api/v1/secrets/GITHUB_TOKEN", headers=token)
    assert one.status_code == 200 and "value" not in one.json()


def test_rbac_viewer_and_operator_are_denied(vault_env: str, api: dict[str, Any]) -> None:
    client = api["client"]
    _create(client, api["token_for"]("admin"))  # seed one
    for role in ("viewer", "operator"):
        token = api["token_for"](role)
        # *:read must NOT reach secret:read (carve-out), and neither may write.
        assert client.get("/api/v1/secrets", headers=token).status_code == 403
        assert _create(client, token, name="OTHER").status_code == 403


def test_maintainer_can_manage(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("maintainer")
    assert _create(client, token).status_code == 201  # §4.2 grants maintainer write


def test_rotate_and_delete_lifecycle(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    _create(client, token)
    rotated = client.post(
        "/api/v1/secrets/GITHUB_TOKEN/rotate", json={"value": "ghp_new"}, headers=token
    )
    assert rotated.status_code == 200
    deleted = client.delete("/api/v1/secrets/GITHUB_TOKEN", headers=token)
    assert deleted.status_code == 204
    assert client.get("/api/v1/secrets/GITHUB_TOKEN", headers=token).status_code == 404


def test_rotate_and_delete_unknown_are_404(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    assert (
        client.post("/api/v1/secrets/NOPE/rotate", json={"value": "x"}, headers=token).status_code
        == 404
    )
    assert client.delete("/api/v1/secrets/NOPE", headers=token).status_code == 404


def test_duplicate_is_409_and_bad_name_is_400(vault_env: str, api: dict[str, Any]) -> None:
    client, token = api["client"], api["token_for"]("admin")
    _create(client, token)
    assert _create(client, token).status_code == 409
    assert _create(client, token, name="../etc/passwd").status_code == 400


def test_unconfigured_vault_answers_503(api: dict[str, Any]) -> None:
    # No vault_env fixture → no age key → no writable store → 503, not a crash.
    client, token = api["client"], api["token_for"]("admin")
    assert _create(client, token).status_code == 503
