"""Schema acceptance tests (TITAN-BCK-008): pytest -m schema."""

import time
import uuid

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

import titan.schema  # noqa: F401 — registers all models on Base.metadata
from tests.conftest import downgrade_base, run_async, upgrade_head
from titan.db import uuid7

pytestmark = pytest.mark.schema

# The Phase-6 15-table data spine…
CORE_TABLES = {
    "users",
    "roles",
    "user_roles",
    "projects",
    "milestones",
    "sprints",
    "agents",
    "tasks",
    "task_dependencies",
    "runs",
    "artifacts",
    "messages",
    "decisions",
    "outbox",
    "notifications",
}
# …plus identity credential tables added in Phase 8 (migration 0004).
IDENTITY_TABLES = {"local_credentials", "refresh_tokens", "api_keys"}
# …plus the events inbox ledger added in Phase 10 (migration 0005).
EVENTS_TABLES = {"event_inbox"}
# …plus LLM accounting + budget tables added in Phase 17 (migration 0007).
LLM_TABLES = {"llm_usage", "budget_holds"}
# …plus the immutable tool_audit ledger added in Phase 18 (migration 0008),
# the tool_approvals records added in Phase 19 (migration 0009), and the
# admin-approved egress hosts added in Phase 23 (migration 0010).
TOOLS_TABLES = {"tool_audit", "tool_approvals", "project_egress_hosts"}
# …plus the conflict-resolution state machine + veto gate + deadlock config added
# in Phase 31 (migration 0012).
CONFLICT_TABLES = {"conflicts", "conflict_transitions", "merge_vetoes", "conflict_thresholds"}
# …plus the Orchestrator watchdog's live config added in Phase 32 (migration 0013).
WATCHDOG_TABLES = {"watchdog_config"}
FLAGS_TABLES = {"feature_flags"}  # Phase 72 (Vol 9 §5.3)
AUDIT_TABLES = {"audit_events", "audit_seals"}  # Phase 73 (Vol 8 §10)
# …plus the per-project delivery-pipeline state machine added in Phase 41
# (migration 0019, Vol 6 §2.1).
PIPELINE_TABLES = {"pipeline_states", "pipeline_stage_events"}
# …plus the handoff artifacts + auditable gate ledger added in Phase 42
# (migration 0020, Vol 6 §2.2–2.3).
HANDOFF_TABLES = {"handoff_artifacts", "gate_evaluations"}
# …plus the versioned constitution + CEO clarification batches added in Phase 43
# (migration 0021, Vol 6 §6, §2.4).
CONSTITUTION_TABLES = {
    "constitution_versions",
    "clarification_batches",
    "clarification_questions",
}
# …plus the self-improvement loop engine added in Phase 44 (migration 0022,
# Vol 6 §3).
LOOP_TABLES = {"loop_records", "loop_cycles", "loop_findings", "loop_stage_events"}
# …plus the local deployment records added in Phase 48 (migration 0025, Vol 7
# §6.7 / TITAN-INT-046 — the titan.local connector's deploy.compose target).
DEPLOY_TABLES = {"deployments"}
# …plus the technical-debt ledger added in Phase 49 (migration 0026, Vol 6 §5 /
# TITAN-WFL-021 — every accepted deviation at review becomes a debt ticket).
DEBT_TABLES = {"debt_tickets"}
# …plus the autonomous-planning mechanics added in Phase 50 (migration 0027, Vol 6
# §4 / TITAN-WFL-016/018/019 — ceremonies, milestone state, blocker ledger).
PLANNING_TABLES = {"sprint_ceremonies", "milestone_states", "task_blocks"}
# …plus the versioned derived project artifacts added in Phase 51 (migration 0028,
# Vol 6 §5 / TITAN-WFL-020 — the six auto-generated artifacts' store).
ARTIFACTS_TABLES = {"derived_artifacts"}
# …plus the manifest-driven plugin runtime added in Phase 52 (migration 0029,
# Vol 7 §3 / TITAN-INT-010..017 — the catalogue + the lifecycle install record).
PLUGIN_TABLES = {"plugins", "plugin_installs"}
# …plus per-user notification channel routing added in Phase 64 (migration 0033,
# Vol 4 §7.1 — the CHANNEL half of the category × channel matrix; the category
# half has been on notifications since 0003). Sparse: a row exists only
# where a user has overridden a §7.1 default.
PREFERENCES_TABLES = {"notification_preferences"}
# …plus the auth-hardening tables added in Phase 67 (migration 0034): the
# security audit ledger, TOTP MFA secret + recovery codes, and the per-org MFA
# policy (TITAN-SEC-015/017).
AUTH_HARDENING_TABLES = {
    "security_events",
    "mfa_credentials",
    "mfa_recovery_codes",
    "org_security_policy",
}
# …plus the secrets vault added in Phase 68 (migration 0035, Vol 8 §6 /
# TITAN-SEC-050/052): secret METADATA (never values — those live age-encrypted,
# unlocked into memory) and the append-only secret-lifecycle + envelope-integrity
# audit ledger.
SECRETS_VAULT_TABLES = {"secret_metadata", "secret_audit"}
EVOLUTION_TABLES = {"evolution_artifacts"}  # Phase 83 (Vol 10 §2, migration 0038)
# NOTE: the Memory/KB/KG tables (Phases 33–39, migrations 0014–0018) live in the
# ``brain`` schema, which inspect().get_table_names() does not enumerate by
# default — so they are intentionally absent from this public-schema census.
EXPECTED_TABLES = (
    CORE_TABLES
    | IDENTITY_TABLES
    | EVENTS_TABLES
    | LLM_TABLES
    | TOOLS_TABLES
    | CONFLICT_TABLES
    | WATCHDOG_TABLES
    | FLAGS_TABLES
    | AUDIT_TABLES
    | PIPELINE_TABLES
    | HANDOFF_TABLES
    | CONSTITUTION_TABLES
    | LOOP_TABLES
    | DEPLOY_TABLES
    | DEBT_TABLES
    | PLANNING_TABLES
    | ARTIFACTS_TABLES
    | PLUGIN_TABLES
    | PREFERENCES_TABLES
    | AUTH_HARDENING_TABLES
    | SECRETS_VAULT_TABLES
    | EVOLUTION_TABLES
)

EXPECTED_INDEXES = {
    "projects": {"idx_projects_status"},
    "agents": {"idx_agents_project_role"},
    "tasks": {"idx_tasks_project_status_priority", "idx_tasks_assignee"},
    "runs": {"idx_runs_task", "idx_runs_status", "uq_runs_single_active_owner"},
    "artifacts": {"idx_artifacts_project_kind"},
    "messages": {
        "idx_messages_thread",
        "idx_messages_project_kind",
        "idx_messages_correlation",
        "idx_messages_to_agent",
    },
    "decisions": {"idx_decisions_project"},
    "outbox": {"idx_outbox_unpublished"},
    "notifications": {"idx_notifications_unread"},
    "tool_audit": {"idx_tool_audit_project_seq", "idx_tool_audit_call", "idx_tool_audit_task"},
    "tool_approvals": {"idx_tool_approvals_project_status", "uq_tool_approvals_pending"},
    "project_egress_hosts": {"uq_project_egress_hosts"},
    "conflicts": {"uq_conflicts_project_cid"},
    "conflict_transitions": {"idx_conflict_transitions_conflict"},
    "merge_vetoes": {"uq_merge_vetoes_active", "idx_merge_vetoes_task"},
    "conflict_thresholds": {"uq_conflict_thresholds_scope"},
    "watchdog_config": {"uq_watchdog_config_scope"},
    # Phase 63. `idx_llm_usage_created_at` backs Monitoring's PLATFORM-scope
    # cost query, which filters on created_at alone — 0007's two indexes both
    # lead with a different column and neither serves it. The other two are
    # pinned here at the same time: they have existed since 0007 with nothing
    # asserting them.
    "llm_usage": {
        "idx_llm_usage_project",
        "idx_llm_usage_task",
        "idx_llm_usage_created_at",
    },
}

PARTIAL_INDEXES = {
    "idx_tasks_assignee",
    "idx_runs_status",
    "idx_outbox_unpublished",
    "idx_notifications_unread",
    "uq_tool_approvals_pending",
    "idx_messages_correlation",
    "idx_messages_to_agent",
    "uq_merge_vetoes_active",
    "idx_merge_vetoes_task",
    "uq_runs_single_active_owner",
}


def test_public_schema_table_census_is_exact(migrated_engine: AsyncEngine) -> None:
    async def tables() -> set[str]:
        async with migrated_engine.connect() as conn:
            return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))

    found = run_async(tables)
    assert found - {"alembic_version"} == EXPECTED_TABLES


def test_conventions_columns_on_every_table(migrated_engine: AsyncEngine) -> None:
    async def check() -> list[str]:
        missing: list[str] = []
        async with migrated_engine.connect() as conn:

            def gather(sc: Connection) -> None:
                inspector = inspect(sc)
                for table in EXPECTED_TABLES:
                    cols = {c["name"] for c in inspector.get_columns(table)}
                    for required in ("created_at", "updated_at", "version"):
                        if required not in cols:
                            missing.append(f"{table}.{required}")

            await conn.run_sync(gather)
        return missing

    assert run_async(check) == []


def test_specified_indexes_exist_with_partials(migrated_engine: AsyncEngine) -> None:
    async def check() -> tuple[set[str], set[str]]:
        found: set[str] = set()
        partial: set[str] = set()
        async with migrated_engine.connect() as conn:

            def gather(sc: Connection) -> None:
                inspector = inspect(sc)
                for table in EXPECTED_TABLES:
                    for index in inspector.get_indexes(table):
                        name = index["name"]
                        assert name is not None
                        found.add(name)
                        if index.get("dialect_options", {}).get("postgresql_where") is not None:
                            partial.add(name)

            await conn.run_sync(gather)
        return found, partial

    found, partial = run_async(check)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= found, f"missing indexes on {table}: {expected - found}"
    assert partial >= PARTIAL_INDEXES


def test_index_predicates_and_ordering_exact(migrated_engine: AsyncEngine) -> None:
    """Pin the partial predicates and DESC ordering via pg_get_indexdef."""
    expectations = {
        "idx_tasks_assignee": ["WHERE", "assigned", "in_progress"],
        "idx_runs_status": ["WHERE", "queued", "running"],
        "idx_outbox_unpublished": ["WHERE", "published_at IS NULL"],
        "idx_notifications_unread": ["WHERE", "read_at IS NULL"],
        "idx_runs_task": ["attempt DESC"],
        "idx_messages_project_kind": ["created_at DESC"],
        "idx_decisions_project": ["created_at DESC"],
        "uq_merge_vetoes_active": ["WHERE", "status = 'active'"],
        "idx_merge_vetoes_task": ["WHERE", "task_id IS NOT NULL", "status = 'active'"],
        "uq_runs_single_active_owner": ["UNIQUE", "WHERE", "queued", "running"],
    }

    async def defs() -> dict[str, str]:
        async with migrated_engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(:names)"),
                {"names": list(expectations)},
            )
            return {str(name): str(definition) for name, definition in rows.fetchall()}

    definitions = run_async(defs)
    for name, fragments in expectations.items():
        assert name in definitions, f"index {name} missing"
        for fragment in fragments:
            assert fragment in definitions[name], (
                f"{name}: expected {fragment!r} in {definitions[name]}"
            )


def test_citext_email_is_case_insensitive(migrated_engine: AsyncEngine) -> None:
    async def check() -> int | None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Case Test', 'local', :subj)"
                ),
                {"id": uuid7(), "email": "Case.Test@Example.COM", "subj": str(uuid.uuid4())},
            )
            hit = await conn.execute(
                text("SELECT 1 FROM users WHERE email = 'case.test@example.com'")
            )
            return hit.scalar()

    assert run_async(check) == 1


def test_updated_at_trigger_fires(migrated_engine: AsyncEngine) -> None:
    user_id = uuid7()

    async def insert() -> str:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Trigger Test', 'local', :subj)"
                ),
                {"id": user_id, "email": f"trigger-{user_id}@titan.local", "subj": str(user_id)},
            )
            row = await conn.execute(
                text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
            )
            return str(row.scalar())

    async def update() -> str:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET display_name = 'Trigger Fired' WHERE id = :id"),
                {"id": user_id},
            )
            row = await conn.execute(
                text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
            )
            return str(row.scalar())

    before = run_async(insert)
    time.sleep(0.02)  # now() is transaction-stable; ensure distinct tx timestamps
    after = run_async(update)
    assert after > before


def test_downgrade_upgrade_round_trip(migrated_engine: AsyncEngine, schema_url: str) -> None:
    downgrade_base(schema_url)

    async def tables() -> set[str]:
        async with migrated_engine.connect() as conn:
            return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))

    stripped = run_async(tables)
    assert stripped - {"alembic_version"} == set()

    upgrade_head(schema_url)
    restored = run_async(tables)
    assert restored - {"alembic_version"} == EXPECTED_TABLES
