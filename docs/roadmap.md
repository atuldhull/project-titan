# TITAN roadmap & explicit deferrals

This is the in-repo record of work **explicitly deferred** past the current build, mapped
to roadmap phases. Nothing here is implemented; each item is a deliberate scope decision.

## Explicitly deferred (Phase 86, Vol 10)

Per the Phase-86 charter, the following are recorded as deferrals and **not built**:

| Item | Ref | Roadmap phase |
|---|---|---|
| Plugin marketplace / registry | Vol 10 §9 | Roadmap Phase 3 |
| SSO / SCIM enterprise identity | TITAN-EVO-033 | Roadmap Phase 4 |
| Multi-tenancy (org isolation) | TITAN-EVO-034 | Roadmap Phase 4 |
| Model fine-tuning pipeline | Vol 10 §11, TITAN-EVO-038..041 | Roadmap Phase 5 |

## Honest gaps carried by Phase 86 (built partially; follow-ups)

The Phase-86 charter assumes a few capabilities the current single-worker / local-model
runtime cannot fully back. The A/B, bench, and debate machinery ships as real, tested
seams; these execution wirings are the documented follow-ups:

- **Live challenger execution.** The A/B harness records + analyzes both arms' scores and
  auto-rolls-back on a KPI-floor breach, but running a *challenger prompt against live task
  traffic* needs a variant-aware prompt resolver (today `EvolutionResolver` serves a single
  fail-closed champion). Follow-up: add a `resolve_prompt(agent_id, variant=...)` path + a
  per-turn composer override (the `runtime.composer` seam is clean).
- **Container-isolated per-case bench execution.** `titan bench run` scores cases through
  the LLM gateway; full disposable-container execution per case reuses the Phase-52 sandbox
  (`SandboxController`) + a `WorkspaceCollector` on `LiveExecutor`. Follow-up: wire the
  collector so the executable tracks (B-CODE) run in a fresh container and capture artifacts.
- **Distinct-model debate offline.** The offline profile has one local model, so debaters
  fall back to distinct personas + per-request temperature (flagged `single_model=true`).
  Genuine multi-model debate needs the cloud profile + provider keys; the code already
  routes per-`model_alias` when a multi-model profile is configured.
- **Dedicated evolution budget ceiling.** Evolution LLM calls are metered off any
  project/task (EVO-020) but fall on the shared org-month cap; a distinct `per_evolution_*`
  budget tier is a follow-up if evolution spend must be bounded independently.
