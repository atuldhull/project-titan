# EP-0001 — Genesis: migrate the roster prompts into the registry

| Field | Value |
|---|---|
| **EP** | EP-0001 |
| **Status** | merged |
| **Author** | Phase 83 build |
| **Motivating RCR(s)** | — (framework bootstrap) |
| **Artifacts changed** | `evolution/prompts/**`, `evolution/routing/policy.yaml`, `evolution/manifest.yaml` |
| **Approved by** | `00000000-0000-0000-0000-000000000083` (genesis bootstrap principal) |
| **Approved at** | 2026-08-03T00:00:00Z |

## Summary

Establishes the prompt registry (TITAN-EVO-015) and the evolution manifest
(TITAN-EVO-002). The 16 core-roster system-prompt templates (plus the shared
`_base` skeleton and the `localization` extensibility sample) were moved verbatim
from `config/prompts/agents/*.j2` into `evolution/prompts/<agent>/system.md` with
YAML front-matter, and pinned by content sha256 in `manifest.yaml`. The routing
policy artifact (`evolution/routing/policy.yaml`) is seeded as "inherit the
operational model-policy". `config/prompts/agents/*.j2` was deleted — the registry
is the single source of truth.

## Rationale

Bootstrap of the Volume 10 evolution safety model: everything TITAN may change
about itself now lives under version control with a human-gated approval flow.

## Change detail

Every migrated file is byte-identical to its `.j2` predecessor except the
`{% extends "agents/_base.j2" %}` line, rewritten to `{% extends "_base/system.md" %}`,
and the prepended front-matter. Each `manifest.yaml` entry pins the file's
sha256 at semver `1.0.0`.

## Risk & monitoring contract

None — a content-preserving migration. The composer renders byte-identical
prompts (proven by the unchanged prompt-composition test suite); the genesis
approval marks the pre-existing roster as the approved champion.

## Rollback plan

`git revert` restores `config/prompts` and the old composer wiring.
