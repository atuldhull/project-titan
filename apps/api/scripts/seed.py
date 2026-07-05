"""Local-dev seed: the five §9.3 roles + a default local admin (Phase 6).

Idempotent — safe to run repeatedly. Permission strings are resource:action
(Vol 3 §9.3); the Phase-9 RBAC engine owns their evaluation semantics.

Usage: uv run --env-file ../../.env python scripts/seed.py
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from titan import clients
from titan.db import uuid7
from titan.identity.models import Role, User
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
        "*:read",
    ],
    "operator": ["task:transition", "run:create", "message:write", "*:read"],
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
    finally:
        await engine.dispose()

    sys.stdout.write("seeded: 5 roles, local admin (admin@titan.local, org-wide owner)\n")


def main() -> None:
    configure_logging(get_settings().log_level, service="titan-seed")
    asyncio.run(seed())


if __name__ == "__main__":
    main()
