# TITAN-AGT-007 — Full-roster offline acceptance record

`agt-007-offline.json` is the acceptance record for **TITAN-AGT-007** ("the full
roster runs on a single laptop under Docker Compose using local models via
Ollama"), produced by:

```
titan acceptance run --profile local-free --confirm --output docs/acceptance/agt-007-offline.json
```

(Ollama running locally; `TITAN_MODEL_POLICY_PROFILE` and the fully-local
`config/models.local.yaml` force every LLM call to Ollama; paid-provider API keys
unset. Point `TITAN_DATABASE_URL` at a **disposable** database — the run writes
projects/messages and does not clean up.)

## What it proves — and what it does not

The record is deliberately honest (Phase 56):

- **`all_roles_ran` / `roster_turns`** — all 16 roster agents (CEO, PM×2,
  Orchestrator, Architect, the four Engineers, DevOps, QA, Security, Reviewer,
  Critic, Documentation, Research, Memory) each ran a **real** `run_turn` on the
  local model. `local_model_exercised` confirms the model was actually reached.
- **`egress_clean` / `egress_verified` / `hosts_contacted`** — an allowlist egress
  guard (loopback + the Ollama host only) observed the run: **zero paid-provider
  egress** (TITAN-FND-054). `offline = egress_clean AND egress_verified`.
- **`kpi`** — the Section-7 KPI report captured via the Phase-55 runner.
- **`pipeline_wiring_completes`** — the delivery/gate machinery reached a released
  pipeline. This proves the **wiring**, NOT that the roster's own outputs built the
  app.
- **`all_roles_produced_valid_output`** — whether every roster turn produced a
  schema-valid message. It is **not** "an app was built": the turns are independent
  (no pipeline/gates/artifacts flow between them). A local 7B does not clear it, and
  it is recorded as-is, never forced.

The literal AGT-007 goal — the model *autonomously completing a running,
test-passing todo app* — is **not** met at this model size; the record reports the
true per-agent outcomes (mostly `escalated`/`fail_open` on invalid structured
output). The engineering that makes the roster run offline with zero paid egress —
the `LiveExecutor` wiring `run_turn` into the delivery pipeline, plus this
acceptance harness — is what Phase 56 delivers and verifies.
