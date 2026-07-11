"""The Knowledge Base (§10.2) and the knowledge graph (§7.1) in schema ``brain``.

Adds, exactly per Vol 5:
- ``brain.kb_documents`` / ``brain.kb_versions`` (§10.2 DDL) — slug-unique,
  versioned KB docs carrying the six ``doc_type``s; only an *approved* version
  becomes current (``approved_by`` required before it is active).
- ``brain.kb_proposals`` — the proposal→approval WORKFLOW state (§10.3): the
  ``{id}`` in ``POST /kb/proposals/{id}/approve`` is a proposal id. Not in the
  §10.2 DDL (which fixes only documents/versions), but the two-step workflow
  needs a durable, routable pending record; kept minimal and append-mostly.
- ``brain.kg_nodes`` / ``brain.kg_edges`` (§7.1 DDL, ADR-05-04) — the typed,
  relational graph (10 node types, 10 edge types) with valid_from/valid_to edge
  history and the (src, edge_type)/(dst, edge_type) traversal indexes.
- ``brain.ingest_outbox.collection`` — the target Qdrant collection for a chunk
  (default ``titan_memory``); KB chunks carry ``titan_kb`` so the SAME §5.1
  embedding pipeline embeds them into the right collection (no bespoke embed
  code, no second pipeline).
- ``ix_memory_records_kb_slug`` — a partial expression index for the pin-by-slug
  (TITAN-BRN-022) and current-version lookups on the materialised KB records.

Hand-written per §6 (autogenerate forbidden); CHECK constraints pin the type
vocabularies the prompt enumerates so a bad enum is a clean 23514, never a
silent free-text node/edge/doc type.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# §10.1 — the six curated content types.
_DOC_TYPES = (
    "coding_standard",
    "architecture_pattern",
    "best_practice",
    "template",
    "decision_record",
    "api_reference_note",
)
# §7.1 — the ten typed node families and ten typed edge families.
_NODE_TYPES = (
    "project",
    "component",
    "decision",
    "pattern",
    "technology",
    "agent",
    "person",
    "incident",
    "lesson",
    "kb_doc",
)
_EDGE_TYPES = (
    "PART_OF",
    "DEPENDS_ON",
    "DECIDED_FOR",
    "SUPERSEDES",
    "APPLIES",
    "DERIVED_FROM",
    "CONSTRAINS",
    "AUTHORED_BY",
    "USES",
    "DOCUMENTED_IN",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # -- brain.kb_documents (§10.2, verbatim + a doc_type CHECK) ----------------
    op.create_table(
        "kb_documents",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),  # 'python-service-standard'
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "status IN ('active','deprecated')", name="ck_kb_documents_status"
        ),
        sa.CheckConstraint(_in_list("doc_type", _DOC_TYPES), name="ck_kb_documents_doc_type"),
        schema="brain",
    )

    # -- brain.kb_versions (§10.2, verbatim) -----------------------------------
    op.create_table(
        "kb_versions",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("body_md", sa.Text, nullable=False),
        sa.Column("change_note", sa.Text, nullable=False),
        sa.Column("proposed_by", sa.Text, nullable=False),  # agent or human id
        sa.Column("approved_by", sa.Text, nullable=True),  # required before active
        # The memory_record backing this version's chunks in titan_kb (null until
        # materialised on approval); lets supersede find the old points to drop.
        sa.Column(
            "record_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_records.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("document_id", "version", name="uq_kb_versions_document_id_version"),
        schema="brain",
    )

    # -- brain.kb_proposals — the §10.3 proposal→approval workflow state --------
    op.create_table(
        "kb_proposals",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # Null document_id = a proposal to create a NEW document; else a new
        # version of an existing one.
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.kb_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body_md", sa.Text, nullable=False),  # the proposed full body (the diff applied)
        sa.Column("change_note", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("proposed_by", sa.Text, nullable=False),
        # 'pending' → 'approved' | 'rejected'; the routing decision is recorded
        # so an audit reconstructs who signed off (TITAN-BRN-021 provenance).
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("requires_human", sa.Boolean, nullable=False),
        sa.Column("near_duplicates", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("decided_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resulting_version", sa.Integer, nullable=True),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')", name="ck_kb_proposals_status"
        ),
        sa.CheckConstraint(_in_list("doc_type", _DOC_TYPES), name="ck_kb_proposals_doc_type"),
        schema="brain",
    )

    # -- brain.kg_nodes (§7.1 DDL, verbatim + a node_type CHECK) ----------------
    op.create_table(
        "kg_nodes",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("node_type", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),  # 'org' | 'project:<uuid>'
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "record_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_records.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("scope", "node_type", "name", name="uq_kg_nodes_scope_type_name"),
        sa.CheckConstraint(_in_list("node_type", _NODE_TYPES), name="ck_kg_nodes_node_type"),
        schema="brain",
    )

    # -- brain.kg_edges (§7.1 DDL, verbatim + an edge_type CHECK) ---------------
    op.create_table(
        "kg_edges",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "src",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dst",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("edge_type", sa.Text, nullable=False),
        sa.Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "valid_from", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("valid_to", TIMESTAMP(timezone=True), nullable=True),  # non-null = historical
        sa.UniqueConstraint(
            "src", "dst", "edge_type", "valid_from", name="uq_kg_edges_src_dst_type_from"
        ),
        sa.CheckConstraint(_in_list("edge_type", _EDGE_TYPES), name="ck_kg_edges_edge_type"),
        schema="brain",
    )
    op.create_index("ix_kg_edges_src_type", "kg_edges", ["src", "edge_type"], schema="brain")
    op.create_index("ix_kg_edges_dst_type", "kg_edges", ["dst", "edge_type"], schema="brain")
    # At most ONE active (valid_to IS NULL) edge per (src, dst, edge_type): makes
    # the §7.3 add-edge idempotency safe BY CONSTRUCTION, not by call ordering —
    # two concurrent structured writes cannot both create the same current edge.
    # History rows (valid_to set) are outside the partial index, so supersede's
    # stamp-then-re-add never conflicts.
    op.create_index(
        "ux_kg_edges_active",
        "kg_edges",
        ["src", "dst", "edge_type"],
        unique=True,
        schema="brain",
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    # -- ingest_outbox.collection: route a chunk's embedding to its collection --
    op.add_column(
        "ingest_outbox",
        sa.Column(
            "collection", sa.Text, nullable=False, server_default=sa.text("'titan_memory'")
        ),
        schema="brain",
    )

    # -- pin-by-slug / current-version lookups on the materialised KB records ---
    # Partial expression index: the query MUST include ``metadata ? 'kb_slug'``
    # for the planner to use it (the ``->>`` equality alone can't prove the key
    # exists), same pattern as ix_memory_records_wm_client_key.
    op.execute(
        "CREATE INDEX ix_memory_records_kb_slug ON brain.memory_records "
        "((metadata ->> 'kb_slug')) WHERE metadata ? 'kb_slug'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS brain.ix_memory_records_kb_slug")
    op.drop_column("ingest_outbox", "collection", schema="brain")
    op.drop_index("ux_kg_edges_active", table_name="kg_edges", schema="brain")
    op.drop_index("ix_kg_edges_dst_type", table_name="kg_edges", schema="brain")
    op.drop_index("ix_kg_edges_src_type", table_name="kg_edges", schema="brain")
    op.drop_table("kg_edges", schema="brain")
    op.drop_table("kg_nodes", schema="brain")
    op.drop_table("kb_proposals", schema="brain")
    op.drop_table("kb_versions", schema="brain")
    op.drop_table("kb_documents", schema="brain")
