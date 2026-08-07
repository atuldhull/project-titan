# ADR-EVO-02 — Offline bench gate on the EP flow

- **Status:** accepted (Phase 86)
- **Context:** Vol 10 §6 (TITAN-EVO-016/017). An Evolution Proposal (EP) changes an
  agent's prompt/config. Before it may enter online A/B, it must be shown *not* to
  regress the champion on a fixed benchmark — otherwise a plausible-looking prompt edit
  could quietly degrade quality and only be caught in production.

## Decision

1. **TITAN-Bench** (`evolution/bench/suite.yaml`) is a fixed, versioned corpus across six
   tracks (B-CODE / B-REVIEW / B-PLAN / B-ARCH / B-SEC / B-REG). Each track has a
   **deterministic scorer** (`titan.evolution.bench`): the model *solves* a case, the
   scorer *grades* the structured response, so scoring is reproducible and CI-safe (no
   model needed to grade). `titan bench run` produces a scored JSON + Markdown report.
   B-REG cases are generated live from the current Root-Cause Records (Phase 85).

2. **The gate** (`evolution/bench/gates.yaml` + `scripts/check_bench.py`) blocks an EP PR
   whose challenger is inferior to the champion on **any** track. Non-inferiority: a track
   passes iff `challenger ≥ max(min_score, champion − margin)`. Security (B-SEC) and
   regression (B-REG) tolerate **zero** margin. A **missing** report on a prompt-changing
   PR fails the gate closed — a gate that can't tell must not wave the EP through.

3. **Wiring:** the EP author runs `titan bench run` on the challenger and commits
   `evolution/bench/reports/challenger.json` on the EP branch; `champion.json` is the
   committed baseline. CI runs `make check-bench` in the `supply-chain` job (alongside
   `check-evolution`). No EP enters online A/B (a `evolution_deployments` row) until it
   passes.

## Consequences

- Scoring being deterministic means the gate is a real merge-blocker, not advisory.
- The champion baseline (`champion.json`) must be refreshed whenever the champion changes
  (the merged challenger becomes the new champion).
- Full per-case *execution* against local models needs Ollama; the smoke subset is
  laptop-runnable (≤15 min). Container-isolated per-case execution reuses the Phase-52
  sandbox and is a documented follow-up (see `docs/roadmap.md`).
