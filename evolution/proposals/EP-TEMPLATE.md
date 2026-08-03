# EP-XXXX — <short title>

> Evolution Proposal (ADR-EVO-01). Copy this file to `EP-<NNNN>-<slug>.md`, fill
> it in, and open a pull request against `/evolution/**`. Agents may open,
> benchmark, and comment on an EP; only a human holding `evolution:approve` may
> merge it (TITAN-EVO-001).

| Field | Value |
|---|---|
| **EP** | EP-XXXX |
| **Status** | draft \| in-review \| approved \| merged \| reverted |
| **Author** | <agent id or human> |
| **Motivating RCR(s)** | <RCR ids, if any — TITAN-EVO-013> |
| **Artifacts changed** | <paths under /evolution/**> |
| **Approved by** | <human user id — filled at merge> |
| **Approved at** | <ISO-8601 — filled at merge> |

## Summary

What changes and why, in two or three sentences.

## Rationale

The evidence: scorecard trends, KPI regressions, reflections, or an RCR that
motivates this change. Link the data.

## Change detail

For each artifact: old → new (semver bump), and the new content sha256 pinned in
`manifest.yaml`.

## Risk & monitoring contract (TITAN-EVO-003)

- KPI floors this change must hold (e.g. task success rate, review acceptance).
- Observation window and the automatic-revert trigger.

## Benchmark evidence (TITAN-EVO-016/017)

Attach the TITAN-Bench report (added by CI in Phase 86). No EP enters online A/B
until it beats or matches the champion on the relevant tracks.

## Rollback plan

`git revert` of this commit restores the prior approved bytes byte-for-byte.
