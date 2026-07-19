"""Settings → Profile / API Keys / Members (Phase 64; Vol 4 §6.13).

Schema-marked: real DB, real RBAC, real JWTs.

The theme of this file is that self-service and administration are DIFFERENT
authorisations, and each has a failure mode worth a test of its own:

* self-service is authorised by the row filter, so the test that matters is that
  one user cannot reach another's rows even with a valid token;
* `member:*` is carved into ``SENSITIVE_RESOURCES``, so the test that matters is
  that ``*:read`` — which every agent carries — does NOT reach it.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from titan.gateway.rbac import SENSITIVE_RESOURCES
from titan.identity.accounts import MemberRolesUpdate

pytestmark = pytest.mark.schema


def _client(api: dict[str, Any]) -> Any:
    return api["client"]


# --- profile -----------------------------------------------------------------


def test_the_profile_returns_the_real_user_row(api: dict[str, Any]) -> None:
    response = _client(api).get("/api/v1/auth/profile", headers=api["token_for"]("admin"))
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "admin"
    assert body["email"].startswith("admin-")
    assert body["roles"] == ["admin"]
    assert body["user_id"] == str(api["user_ids"]["admin"])


def test_the_profile_has_no_avatar_field(api: dict[str, Any]) -> None:
    # There is no avatar column, no blob store and no upload path in TITAN.
    # §6.13 lists one; asserting its ABSENCE keeps a later reader from adding a
    # UI affordance for a field the API would silently drop.
    body = _client(api).get("/api/v1/auth/profile", headers=api["token_for"]("viewer")).json()
    assert "avatar" not in body and "avatar_url" not in body


def test_a_viewer_can_edit_their_own_display_name(api: dict[str, Any]) -> None:
    # Self-service is not gated on a permission: the lowest-privilege role must
    # still be able to rename itself.
    response = _client(api).patch(
        "/api/v1/auth/profile",
        headers=api["token_for"]("viewer"),
        json={"display_name": "Renamed Person"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Renamed Person"


def test_the_email_cannot_be_changed(api: dict[str, Any]) -> None:
    # Email is the login identity and the unique key; there is no verification
    # flow, so the field is rejected rather than silently ignored.
    before = _client(api).get("/api/v1/auth/profile", headers=api["token_for"]("owner")).json()
    response = _client(api).patch(
        "/api/v1/auth/profile",
        headers=api["token_for"]("owner"),
        json={"display_name": "Owner", "email": "attacker@evil.test"},
    )
    assert response.status_code == 422
    after = _client(api).get("/api/v1/auth/profile", headers=api["token_for"]("owner")).json()
    assert after["email"] == before["email"]


def test_an_empty_display_name_is_rejected(api: dict[str, Any]) -> None:
    response = _client(api).patch(
        "/api/v1/auth/profile",
        headers=api["token_for"]("admin"),
        json={"display_name": ""},
    )
    assert response.status_code == 422


# --- API keys ----------------------------------------------------------------


def _mint_key(api: dict[str, Any], role: str, name: str) -> dict[str, Any]:
    response = _client(api).post(
        "/api/v1/auth/api-keys", headers=api["token_for"](role), json={"name": name}
    )
    assert response.status_code == 201
    result: dict[str, Any] = response.json()
    return result


def test_a_minted_key_is_listed_without_its_secret(api: dict[str, Any]) -> None:
    created = _mint_key(api, "maintainer", "ci-runner")
    listed = (
        _client(api).get("/api/v1/auth/api-keys", headers=api["token_for"]("maintainer")).json()
    )
    mine = [k for k in listed if k["id"] == created["id"]]
    assert len(mine) == 1
    # The secret is shown exactly once, at creation (§9.2).
    assert "api_key" not in mine[0]
    assert mine[0]["prefix"].startswith("titan_ak_")
    assert created["api_key"] not in str(listed)


def test_revoking_a_key_stops_it_authenticating(api: dict[str, Any]) -> None:
    # `api_keys.revoked_at` was already CHECKED by both auth paths but had no
    # writer, so a minted key used to be immortal. This is the whole point of
    # the endpoint: the effect must be immediate, not merely recorded.
    created = _mint_key(api, "admin", "to-be-revoked")
    raw = created["api_key"]

    before = _client(api).get("/api/v1/auth/profile", headers={"X-API-Key": raw})
    assert before.status_code != 401

    revoked = _client(api).delete(
        f"/api/v1/auth/api-keys/{created['id']}", headers=api["token_for"]("admin")
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    after = _client(api).post("/api/v1/auth/token", json={"grant_type": "api_key", "api_key": raw})
    assert after.status_code == 401


def test_one_user_cannot_revoke_another_users_key(api: dict[str, Any]) -> None:
    # The ownership filter is inside the UPDATE, so this is 404 (the key is not
    # yours, therefore it does not exist to you) rather than 403 — which would
    # confirm the id is real.
    created = _mint_key(api, "owner", "owners-key")
    response = _client(api).delete(
        f"/api/v1/auth/api-keys/{created['id']}", headers=api["token_for"]("viewer")
    )
    assert response.status_code == 404

    still_listed = (
        _client(api).get("/api/v1/auth/api-keys", headers=api["token_for"]("owner")).json()
    )
    assert [k for k in still_listed if k["id"] == created["id"]][0]["revoked_at"] is None


def test_revoking_twice_is_idempotent(api: dict[str, Any]) -> None:
    created = _mint_key(api, "admin", "double-revoke")
    first = _client(api).delete(
        f"/api/v1/auth/api-keys/{created['id']}", headers=api["token_for"]("admin")
    )
    second = _client(api).delete(
        f"/api/v1/auth/api-keys/{created['id']}", headers=api["token_for"]("admin")
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


def test_an_agent_token_cannot_manage_its_owners_keys(api: dict[str, Any]) -> None:
    # An API key authenticates as kind="agent" with sub = the HUMAN who minted
    # it. Without the kind check, handing a key to an agent would hand it the
    # ability to enumerate and revoke every other credential of its owner.
    created = _mint_key(api, "admin", "agent-key")
    headers = {"X-API-Key": created["api_key"]}

    assert _client(api).get("/api/v1/auth/api-keys", headers=headers).status_code == 403
    assert _client(api).get("/api/v1/auth/sessions", headers=headers).status_code == 403
    assert (
        _client(api)
        .patch("/api/v1/auth/profile", headers=headers, json={"display_name": "hijacked"})
        .status_code
        == 403
    )


# --- sessions ----------------------------------------------------------------


def _seed_family(api: dict[str, Any], role: str, *, hops: int, revoked: bool = False) -> uuid.UUID:
    """Insert one refresh-token FAMILY with ``hops`` rotation rows.

    Written straight to the table rather than driven through login+refresh
    because local password credentials are not seeded for these fixture users.
    The rows are the real shape the rotation code writes, which is what the
    grouping under test actually reads.
    """
    from titan.db import uuid7

    family_id = uuid7()

    async def insert_rows() -> None:
        async with api["engine"].begin() as conn:
            for hop in range(hops):
                await conn.execute(
                    text(
                        "INSERT INTO refresh_tokens "
                        "(id, family_id, user_id, token_hash, expires_at, revoked_at) VALUES "
                        "(:id, :fam, :uid, :hash, now() + interval '7 days', "
                        " CASE WHEN :revoked THEN now() ELSE NULL END)"
                    ),
                    {
                        "id": uuid7(),
                        "fam": family_id,
                        "uid": api["user_ids"][role],
                        "hash": f"hash-{family_id}-{hop}",
                        # Only the LAST hop carries revoked_at, mirroring how a
                        # revocation actually lands — which is exactly why the
                        # aggregate has to be max(), not min().
                        "revoked": revoked and hop == hops - 1,
                    },
                )

    from tests.conftest import run_async

    run_async(insert_rows)
    return family_id


def test_sessions_are_families_not_rows(api: dict[str, Any]) -> None:
    """One login refreshed twice is ONE session, not three."""
    _seed_family(api, "admin", hops=3)
    _seed_family(api, "admin", hops=1)

    sessions = _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("admin")).json()
    assert len(sessions) == 2, "rotation must not multiply the session list"


def test_a_family_is_revoked_if_any_hop_is(api: dict[str, Any]) -> None:
    # `_seed_family` stamps ONLY the last hop, so this pins that one revoked row
    # out of three makes the whole family read revoked — the behaviour that
    # matters, since revocation lands per-family but row-by-row.
    family_id = _seed_family(api, "owner", hops=3, revoked=True)
    live = _seed_family(api, "owner", hops=3, revoked=False)

    sessions = _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("owner")).json()
    by_family = {s["family_id"]: s for s in sessions}
    assert by_family[str(family_id)]["revoked"] is True
    # The contrast is what makes this non-vacuous: an identically-shaped family
    # with no stamped hop must read live.
    assert by_family[str(live)]["revoked"] is False


def test_another_users_session_is_invisible_and_unrevokable(api: dict[str, Any]) -> None:
    family_id = _seed_family(api, "owner", hops=1)

    listed = _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("viewer")).json()
    assert str(family_id) not in [s["family_id"] for s in listed]

    response = _client(api).delete(
        f"/api/v1/auth/sessions/{family_id}", headers=api["token_for"]("viewer")
    )
    assert response.status_code == 404

    still = _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("owner")).json()
    assert [s for s in still if s["family_id"] == str(family_id)][0]["revoked"] is False


def test_revoking_a_session_marks_the_whole_family(api: dict[str, Any]) -> None:
    family_id = _seed_family(api, "maintainer", hops=2)
    response = _client(api).delete(
        f"/api/v1/auth/sessions/{family_id}", headers=api["token_for"]("maintainer")
    )
    assert response.status_code == 204

    sessions = (
        _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("maintainer")).json()
    )
    assert [s for s in sessions if s["family_id"] == str(family_id)][0]["revoked"] is True


def test_a_session_list_starts_empty_for_a_minted_token(api: dict[str, Any]) -> None:
    # `token_for` mints an access token directly, without a refresh family, so
    # there is nothing to list. Asserting this pins that a session is a REFRESH
    # family — not "any token that exists".
    sessions = _client(api).get("/api/v1/auth/sessions", headers=api["token_for"]("viewer")).json()
    assert sessions == []


def test_revoking_a_session_that_is_not_yours_is_a_404(api: dict[str, Any]) -> None:
    response = _client(api).delete(
        f"/api/v1/auth/sessions/{uuid.uuid4()}", headers=api["token_for"]("admin")
    )
    assert response.status_code == 404


# --- members -----------------------------------------------------------------


def test_member_is_a_sensitive_resource(api: dict[str, Any]) -> None:
    # The carve-out is the ONLY thing stopping `*:read` from granting
    # member:read. If this ever regresses, every agent can enumerate the
    # humans in the org, so it is asserted directly rather than only through
    # the endpoint.
    assert "member" in SENSITIVE_RESOURCES
    assert "settings" in SENSITIVE_RESOURCES


@pytest.mark.parametrize("role", ["viewer", "maintainer", "operator"])
def test_a_non_admin_cannot_list_members(api: dict[str, Any], role: str) -> None:
    # Every one of these bundles carries `*:read`. If member were not carved
    # out, all three would be 200.
    response = _client(api).get("/api/v1/members", headers=api["token_for"](role))
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_admin_can_list_members(api: dict[str, Any], role: str) -> None:
    response = _client(api).get("/api/v1/members", headers=api["token_for"](role))
    assert response.status_code == 200
    emails = {m["email"] for m in response.json()}
    assert len(emails) >= 5


def test_the_roles_endpoint_reports_the_real_five_not_the_specs_four(
    api: dict[str, Any],
) -> None:
    # Vol 4 §6.13 says owner/admin/developer/viewer. The DB defines
    # owner/admin/maintainer/operator/viewer. Offering `developer` in a picker
    # would produce assignments the API rejects.
    roles = _client(api).get("/api/v1/roles", headers=api["token_for"]("admin")).json()
    assert "developer" not in roles
    assert {"owner", "admin", "maintainer", "operator", "viewer"} <= set(roles)


def test_an_admin_can_change_a_members_org_roles(api: dict[str, Any]) -> None:
    target = api["user_ids"]["viewer"]
    response = _client(api).put(
        f"/api/v1/members/{target}/roles",
        headers=api["token_for"]("admin"),
        json={"roles": ["maintainer"]},
    )
    assert response.status_code == 200
    assert response.json()["roles"] == ["maintainer"]


def test_an_unknown_role_is_a_422(api: dict[str, Any]) -> None:
    response = _client(api).put(
        f"/api/v1/members/{api['user_ids']['viewer']}/roles",
        headers=api["token_for"]("admin"),
        json={"roles": ["developer"]},
    )
    assert response.status_code == 422
    assert "developer" in response.json()["detail"]


def _leave_one_owner(api: dict[str, Any], keep: uuid.UUID) -> None:
    """Strip the org-wide owner grant from everyone except ``keep``.

    The DB is SESSION-scoped and the ``api`` fixture seeds a fresh set of users
    on every test, so by the time this file's later tests run the org holds one
    owner per test that has already executed. "Last owner" is an org-wide
    property, so without this the check correctly finds other owners and the
    assertion tests nothing. Passing in isolation and failing in-suite (or the
    reverse) is the recurring shape of this bug — see Phase 63.
    """

    async def strip() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM user_roles ur USING roles r "
                    "WHERE ur.role_id = r.id AND r.name = 'owner' "
                    "AND ur.project_id IS NULL AND ur.user_id <> :keep"
                ),
                {"keep": keep},
            )

    from tests.conftest import run_async

    run_async(strip)


def test_the_last_owner_cannot_be_demoted(api: dict[str, Any]) -> None:
    # Nothing re-seeds a live database, and only `owner` holds "*". An org with
    # no owner is an unrecoverable lockout, so the write is refused.
    owner = api["user_ids"]["owner"]
    _leave_one_owner(api, owner)
    response = _client(api).put(
        f"/api/v1/members/{owner}/roles",
        headers=api["token_for"]("owner"),
        json={"roles": ["viewer"]},
    )
    assert response.status_code == 409

    # And the refusal is real, not cosmetic: the grant survives.
    after = _client(api).get("/api/v1/members", headers=api["token_for"]("owner")).json()
    assert "owner" in [m for m in after if m["user_id"] == str(owner)][0]["roles"]


def test_the_last_owner_check_counts_other_owners(api: dict[str, Any]) -> None:
    # With a SECOND owner in place the first may step down — the guard is
    # "the org keeps an owner", not "this user is special".
    owner = api["user_ids"]["owner"]
    _leave_one_owner(api, owner)
    promoted = _client(api).put(
        f"/api/v1/members/{api['user_ids']['maintainer']}/roles",
        headers=api["token_for"]("owner"),
        json={"roles": ["owner"]},
    )
    assert promoted.status_code == 200
    stepped_down = _client(api).put(
        f"/api/v1/members/{owner}/roles",
        headers=api["token_for"]("owner"),
        json={"roles": ["admin"]},
    )
    assert stepped_down.status_code == 200


def test_a_project_grant_does_not_disturb_org_roles(api: dict[str, Any]) -> None:
    """Writing at project scope must leave the org-wide set alone."""

    async def make_project() -> str:
        from titan.db import uuid7

        pid = uuid7()
        async with api["engine"].begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, created_by) "
                    "VALUES (:id, :name, 'Grant Target', 'fixture', :owner)"
                ),
                {"id": pid, "name": f"grants-{pid}", "owner": api["user_ids"]["owner"]},
            )
        return str(pid)

    from tests.conftest import run_async

    project_id = run_async(make_project)
    target = api["user_ids"]["viewer"]

    before = _client(api).get("/api/v1/members", headers=api["token_for"]("admin")).json()
    org_roles_before = [m["roles"] for m in before if m["user_id"] == str(target)][0]

    response = _client(api).put(
        f"/api/v1/members/{target}/roles",
        headers=api["token_for"]("admin"),
        json={"roles": ["maintainer"], "project_id": project_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["roles"] == org_roles_before, "org-wide grants must be untouched"
    assert {"role": "maintainer", "project_id": project_id} in body["project_grants"]


def test_the_update_model_defaults_to_org_scope() -> None:
    assert MemberRolesUpdate(roles=["admin"]).project_id is None
