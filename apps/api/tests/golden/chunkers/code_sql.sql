-- A representative SQL source fixture for the syntax-aware code chunker.
-- Several DDL statements; each whole statement is a unit and is never split,
-- including the long CREATE TABLE with many columns and constraints.

CREATE TABLE memory_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('episodic', 'semantic', 'procedural')),
    scope text NOT NULL,
    project_id uuid REFERENCES projects (id),
    source_task_id uuid REFERENCES tasks (id),
    source_agent text NOT NULL,
    title text NOT NULL,
    body_md text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    importance real NOT NULL DEFAULT 0.5,
    decay_score real NOT NULL DEFAULT 1.0,
    access_count integer NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'pruned')),
    superseded_by uuid REFERENCES memory_records (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_memory_records_scope_kind_status ON memory_records (scope, kind, status);

CREATE INDEX ix_memory_records_project ON memory_records (project_id);

CREATE TABLE memory_chunks (
    id uuid PRIMARY KEY,
    record_id uuid NOT NULL REFERENCES memory_records (id) ON DELETE CASCADE,
    seq integer NOT NULL,
    text text NOT NULL,
    token_count integer NOT NULL,
    embed_model text,
    embedded_at timestamptz,
    UNIQUE (record_id, seq)
);

CREATE INDEX ix_memory_chunks_record ON memory_chunks (record_id);

CREATE TABLE ingest_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id uuid NOT NULL REFERENCES memory_chunks (id) ON DELETE CASCADE,
    op text NOT NULL CHECK (op IN ('upsert', 'delete')),
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

CREATE INDEX ix_ingest_outbox_pending ON ingest_outbox (enqueued_at) WHERE processed_at IS NULL;

CREATE TABLE memory_ledger (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    actor_agent text NOT NULL,
    task_id uuid REFERENCES tasks (id),
    action text NOT NULL CHECK (action IN ('read', 'write', 'promote', 'prune', 'supersede')),
    record_ids uuid[] NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX ix_memory_ledger_records ON memory_ledger USING gin (record_ids);
