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
        # Same shape, same reason (Phase 62, Vol 4 §6.7): `logs` is carved into
        # SENSITIVE_RESOURCES, so the system-log history endpoint needs an
        # explicit grant. Owner + Admin only — a viewer must not get the
        # firehose, and neither must an agent (every agent runs as `operator`,
        # whose bundle carries *:read).
        "logs:read",
        # Resolve admin-risk tool approvals (Vol 7 §4.4, TITAN-INT-022).
        "tool_approval:write",
        # Write durable memory (Vol 5 Memory Service, Phase 33).
        "memory:write",
        # rebuild-index and other memory maintenance (TITAN-BRN-004, Phase 35).
        "memory:admin",
        # Propose + approve KB changes, including the human sign-off on protected
        # doc types (coding_standard/decision_record) — Vol 5 §10.3, Phase 39.
        "kb:propose",
        "kb:approve",
        # Drive the delivery pipeline + the CEO NOT_APPLICABLE / budget-raise
        # override (Vol 6 §2.1/§2.5, Phase 41).
        "pipeline:advance",
        "pipeline:override",
        # Install/enable plugins and grant them their declared capabilities. This is an
        # operator act — never an agent's — so it lives only with Admin/Owner
        # (Vol 7 §3.3, ADR-INT-04). plugin:read is reachable via *:read for everyone.
        "plugin:admin",
        # Org administration (Vol 4 §6.13, Phase 64). Both resources are carved
        # into SENSITIVE_RESOURCES, so — exactly like audit:read and logs:read
        # above — `*:read` no longer reaches them and the grant must be
        # explicit. Owner (via "*") and Admin only: `member` exposes the org's
        # real people, and `settings` re-points every agent's LLM traffic.
        "member:read",
        "member:write",
        "settings:read",
        "settings:write",
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
        # Propose + approve KB changes (human sign-off on protected types), §10.3.
        "kb:propose",
        "kb:approve",
        # Maintainer advances the pipeline; the NOT_APPLICABLE skip + budget-raise
        # (pipeline:override) stay with Admin/Owner — the human sign-off gate.
        "pipeline:advance",
        "*:read",
    ],
    # The machine/agent bundle (TITAN-BCK-021): task flow + the sandbox
    # side-effect scopes the fs/terminal/git/code tools require (Phases 20-21),
    # plus the docker/browser scopes for the container + E2E tools (Phase 22).
    "operator": [
        "task:transition",
        "run:create",
        "message:write",
        # Agents write durable memory through the Memory Service (Vol 5, Phase 33).
        "memory:write",
        # Agents PROPOSE KB changes and a Code-Reviewer agent APPROVES non-protected
        # types (§10.3); the human sign-off on protected types blocks agent approval.
        "kb:propose",
        "kb:approve",
        # Agents advance the delivery pipeline; the NOT_APPLICABLE skip stays a
        # human/override authority (Vol 6 §2.2, Phase 41).
        "pipeline:advance",
        "workspace:read",
        "workspace:write",
        "terminal:run",
        "docker:run",
        "browser:use",
        "web:search",
        "web:fetch",
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
