-- Schema dump for the conflict subsystem.
-- The table chunker groups whole statements and never splits one.

CREATE TABLE conflict_transitions (id uuid PRIMARY KEY, dispute_key text NOT NULL, from_state text NOT NULL, to_state text NOT NULL, actor text NOT NULL, at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_conflict_transitions_dispute ON conflict_transitions (dispute_key);

CREATE TABLE conflict_vetoes (id uuid PRIMARY KEY, dispute_key text NOT NULL, raised_by text NOT NULL, task_id uuid REFERENCES tasks (id), reason text NOT NULL, at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_conflict_vetoes_task ON conflict_vetoes (task_id);

CREATE TABLE decision_log (id uuid PRIMARY KEY, dispute_key text NOT NULL, decided_by text NOT NULL, decision text NOT NULL, rationale text NOT NULL, at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_decision_log_dispute ON decision_log (dispute_key);

CREATE TABLE arbitration_appointments (id uuid PRIMARY KEY, dispute_key text NOT NULL, dispute_kind text NOT NULL, arbiter text NOT NULL, appointed_at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_arbitration_appointments_dispute ON arbitration_appointments (dispute_key);

CREATE TABLE escalations (id uuid PRIMARY KEY, dispute_key text NOT NULL, cause text NOT NULL, target text NOT NULL, raised_at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_escalations_dispute ON escalations (dispute_key);

CREATE TABLE deadlock_markers (id uuid PRIMARY KEY, dispute_key text NOT NULL, round integer NOT NULL, reason text NOT NULL, at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_deadlock_markers_dispute ON deadlock_markers (dispute_key);

CREATE TABLE human_overrides (id uuid PRIMARY KEY, dispute_key text NOT NULL, operator text NOT NULL, decision text NOT NULL, justification text NOT NULL, at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ix_human_overrides_dispute ON human_overrides (dispute_key);

CREATE TABLE veto_audit (id uuid PRIMARY KEY, veto_id uuid NOT NULL REFERENCES conflict_vetoes (id), event text NOT NULL, at timestamptz NOT NULL DEFAULT now());

ALTER TABLE conflict_transitions ADD COLUMN round integer NOT NULL DEFAULT 0;

ALTER TABLE conflict_vetoes ADD COLUMN resolved_at timestamptz;

ALTER TABLE decision_log ADD COLUMN superseded_by uuid REFERENCES decision_log (id);
