# Jira sync connector (Vol 7 §6.4, TITAN-INT-043)

Mirrors TITAN tasks into Jira issues (outbound) and turns human Jira activity into TITAN events
(inbound), as a **pure plugin** — zero core change (INT-011: deleting this directory leaves core
tests green).

## What this connector owns (zero-core, tested)

- **Tracker I/O** — `jira.*` tools: `create_issue` (stamps a `titan-task-<id>` label = the durable
  per-issue side of the mapping), `transition_issue`, `add_comment`, `get_issue`, `find_by_task`
  (JQL label search — resolves a TITAN task → its issue without a separate table).
- **Inbound translation** — `jira:issue_updated` / `comment_created` → `task.comment_added` /
  `task.remote_status_changed`, keyed by the `titan-task-` label (`jira_events.translate`).
- **The sync ALGORITHM** — `jira_sync.resolve_status`: the TITAN-state ⇄ tracker-status map plus
  the §6.4 conflict rule (last-writer-wins when one side moved; a **simultaneous transition to
  different states is flagged `conflict`** for the PM agent, never silently overwritten).

## Honest scope — the orchestration gap

Full two-way sync per §6.4 needs a **per-project mapping/last-synced store** and the ability to
**read live TITAN task state** to feed `resolve_status`. An out-of-process connector has neither —
its capabilities are only `secret` / `publish` / `fetch` / `read_file` (no storage, no task-read),
and a core mapping table would violate INT-011. So the two-way **orchestration** (who calls
`resolve_status` with what, and how the mapping/last-synced is persisted, and the 60 s soak per
INT-043) belongs to a **core-side sync engine** (reads tasks, persists the mapping, calls this
connector for the tracker I/O and `resolve_status` for the decision). This connector supplies the
pieces that engine needs; the engine itself is out of scope for a zero-core connector and is the
documented gap. The `titan-task-` label design means the mapping is recoverable from the tracker
even without a persisted table.

## Webhook signature

The manifest declares core's only mode, `hmac-sha256` (a bad signature → core 401). Jira webhooks
are unsigned by default, so the operator fronts them with a thin proxy that adds
`x-hub-signature-256 = HMAC(JIRA_WEBHOOK_SECRET, body)` — `translate()` + core's verify are
agnostic once the frame arrives. (Core has no `verify: token` mode by design; adding one is a
core-webhook-framework decision, not a connector change.)
