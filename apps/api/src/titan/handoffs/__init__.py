"""The handoff executor, gate ledger, and the twelve versioned artifact schemas
(Vol 6 §2.2–2.3, TITAN-WFL-003/004/005). Handoffs ARE the Phase-41 pipeline's
stage transitions — this module validates each stage's artifact, evaluates its
acceptance gate, records the gate ledger, and advances / retries / escalates the
pipeline. Only ``service`` is importable across the module boundary."""
