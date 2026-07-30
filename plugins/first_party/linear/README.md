# Linear sync connector (Vol 7 §6.4, TITAN-INT-043)

The GraphQL sibling of the [Jira connector](../jira/README.md) — mirrors TITAN tasks into Linear
issues and turns human Linear activity into TITAN events, as a **pure plugin** (zero core change,
INT-011).

## What this connector owns (zero-core, tested)

- **Tracker I/O** — `linear.*` tools over the GraphQL API: `create_issue` (embeds a
  `[titan-task:<id>]` marker in the description = the durable per-issue mapping), `update_issue`
  (a `stateId` update = a status transition), `add_comment`, `get_issue`, `find_by_task`
  (filters issues whose description contains the marker).
- **Inbound translation** — Linear webhooks (`{action,type,data,updatedFrom}`) → `task.comment_added`
  / `task.remote_status_changed`, keyed by the description marker.
- **The sync ALGORITHM** — `linear_sync.resolve_status`: the same state-map + `sync_conflict`
  rule as Jira (last-writer-wins when one side moved; a simultaneous conflicting transition is
  flagged, not overwritten).

## Honest scope — the orchestration gap

Identical to the Jira connector: the two-way **orchestration** (persisting the per-project
mapping/last-synced marker, reading live TITAN task state to feed `resolve_status`, the 60 s soak
per INT-043) needs a **core sync engine** — an out-of-process connector has only `secret` /
`publish` / `fetch` / `read_file` (no storage, no task-read), and a core mapping table would
violate INT-011. This connector supplies the tracker I/O, the translation, and the decision
algorithm that engine calls. See the [Jira README](../jira/README.md) for the full rationale.

## Webhook signature

Linear **does** HMAC-SHA256 the body — but in a `Linear-Signature` header, not the
`x-hub-signature-256` core reads (its only mode). A thin proxy copies the header (same scheme +
secret), or a future core `verify` header-name option closes it; `translate()` is agnostic.
