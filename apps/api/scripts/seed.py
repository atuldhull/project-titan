"""Local-dev seed: the five §9.3 roles + a default local admin (Phase 6).

Idempotent — safe to run repeatedly. Permission strings are resource:action
(Vol 3 §9.3); the Phase-9 RBAC engine owns their evaluation semantics.

Usage: uv run --env-file ../../.env python scripts/seed.py
"""

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from titan import clients
from titan.db import uuid7
from titan.identity.models import Role, User
from titan.identity.service import set_local_password
from titan.observability.logging import configure_logging
from titan.settings import get_settings

# §9.3 role → permission bundles. maintainer gets task:* "except reopen",
# encoded as the explicit task actions minus task:reopen.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "owner": ["*"],
    "admin": [
        "project:*",
        "agent:configure",
        "task:*",
        "run:cancel",
        "run:create",
        "message:write",
        "decision:write",
        # audit:read is explicit: *:read no longer reaches the audit log
        # (Vol 8 §4.2 grants audit read to Owner + Admin only).
        "audit:read",
        # Resolve admin-risk tool approvals (Vol 7 §4.4, TITAN-INT-022).
        "tool_approval:write",
        "*:read",
    ],
    "maintainer": [
        "project:write",
        "task:create",
        "task:update",
        "task:transition",
        "task:assign",
        "task:cancel",
        "decision:write",
        "message:write",
        # Vol 8 §4.2: Maintainer resolves approval-workflow ("A") actions.
        "tool_approval:write",
        "*:read",
    ],
    # The machine/agent bundle (TITAN-BCK-021): task flow + the sandbox
    # side-effect scopes the fs/terminal/git/code tools require (Phases 20-21),
    # plus the docker/browser scopes for the container + E2E tools (Phase 22).
    "operator": [
        "task:transition",
        "run:create",
        "message:write",
        "workspace:read",
        "workspace:write",
        "terminal:run",
        "docker:run",
        "browser:use",
        "*:read",
    ],
    "viewer": ["*:read"],
}

ADMIN_EMAIL = "admin@titan.local"

# Plain SQL: the conflict target is the NULLS NOT DISTINCT unique key that
# includes the nullable project_id column.
_GRANT_OWNER = text(
    """
    INSERT INTO user_roles (id, user_id, role_id, project_id)
    SELECT :id, u.id, r.id, NULL
    FROM users u, roles r
    WHERE u.email = :email AND r.name = 'owner'
    ON CONFLICT (user_id, role_id, project_id) DO NOTHING
    """
)


async def seed() -> None:
    settings = get_settings()
    engine = clients.create_engine(settings)
    try:
        async with engine.begin() as conn:
            for name, permissions in ROLE_PERMISSIONS.items():
                await conn.execute(
                    insert(Role)
                    .values(id=uuid7(), name=name, permissions=permissions)
                    .on_conflict_do_update(
                        index_elements=[Role.name], set_={"permissions": permissions}
                    )
                )

            await conn.execute(
                insert(User)
                .values(
                    id=uuid7(),
                    email=ADMIN_EMAIL,
                    display_name="Local Admin",
                    auth_provider="local",
                    auth_subject="admin",
                )
                .on_conflict_do_nothing(index_elements=[User.email])
            )

            await conn.execute(_GRANT_OWNER, {"id": uuid7(), "email": ADMIN_EMAIL})

            admin_id = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :email"), {"email": ADMIN_EMAIL}
                )
            ).scalar_one()
            password = os.environ.get("TITAN_BOOTSTRAP_ADMIN_PASSWORD")
            if password is None:
                if settings.environment != "local":
                    raise SystemExit(
                        "refusing to seed the default admin password outside local; "
                        "set TITAN_BOOTSTRAP_ADMIN_PASSWORD"
                    )
                password = "titan-local-admin"
            await set_local_password(conn, admin_id, password)
    finally:
        await engine.dispose()

    sys.stdout.write(
        "seeded: 5 roles, local admin (admin@titan.local, org-wide owner, "
        "password from TITAN_BOOTSTRAP_ADMIN_PASSWORD or dev default)\n"
    )


def main() -> None:
    configure_logging(get_settings().log_level, service="titan-seed")
    asyncio.run(seed())


if __name__ == "__main__":
    main()
