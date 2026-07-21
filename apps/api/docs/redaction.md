# The redaction pipeline (Phase 69 — TITAN-SEC-054/055/046)

One engine (`titan.redaction`), three detectors, wired into every egress
surface where secret text could leave the platform's trust boundary.

## What Vol 8 §6.4 asked for vs. what exists

The phase brief assumes a redaction pipeline is bolted onto three sinks and a
file-walking RAG ingester. Scouting found: (a) the ToolBus has had an
injectable redactor since Phase 18 covering envelopes (results/stderr/
artifacts/errors) — but pattern-only, with no inventory knowledge; (b) log
redaction did NOT exist (`gateway/rbac.py` documented Vol 9 §6.3's claim as
false); (c) nothing anywhere ingests FILES into Qdrant — the Phase-35
pipeline embeds Postgres text chunks, and repo files reach context only
through the tool layer. The implementation follows that reality.

## The engine (`titan.redaction` — an enforced leaf)

- **inventory** — exact match against live vault values via hashed lookup.
  The engine holds SHA-256 digests, never plaintext; candidate tokens from
  the text are hashed and checked. Fed by `titan.vault.inventory` from
  `secret_metadata` names + `store.names()` (age mode) resolved through the
  provider — works in BOTH vault modes. Values shorter than 8 chars are never
  indexed (redacting `"true"` everywhere would destroy the logs). The
  inventory tokenizer (`_INVENTORY_TOKEN_RE`) breaks only on **structural**
  separators (whitespace, quotes, `=`, `,`, `;`, brackets), NOT on
  intra-value punctuation (`. : @ ! - _`), so a punctuated vaulted value —
  a DB password, `id:secret`, a SendGrid `SG.<id>.<key>`, or the value half
  of `KEY=value` — is tested whole against the digest. (The narrow
  base64-alphabet tokenizer used before this fix split such values into
  fragments that never matched → a silent fail-open; Phase-69 review.)
- **patterns** — provider-key shapes incl. TITAN's own `titan_ak_`/`titan_rt_`
  and `AGE-SECRET-KEY-1…`. Supersedes the Phase-18 `_SECRET_VALUES` list.
- **entropy** — ≥24-char, ≥4.0 bits/char tokens, with guards for UUIDs,
  pure hex (trace ids, digests), SRI hashes, digit-less identifiers, and
  path-like tokens (see `entropy.py`). Over-redaction is accepted;
  under-redaction is the failure mode that counts.

Replacement: `[REDACTED:<class>:<hash8>]`, hash8 = sha256(match)[:8] — the
same secret yields the same marker at every internal sink, so audit rows
correlate without revealing values (SEC-092). Overlapping matches merge to
their union so a partially-overlapped secret can't leave a fragment behind.
The **gateway** sink (provider egress) emits a class-only `[REDACTED:<class>]`
with NO hash: the hash is an internal correlator and handing a stable,
unsalted 32-bit fingerprint of a secret to a third-party LLM provider is a
needless confirmation oracle (Phase-69 review).

The provider-key patterns are all **upper-bounded and possessive** (`{lo,hi}+`)
— the engine runs `finditer` for every pattern on every string it redacts, and
log lines / sandbox output are attacker-influenceable, so an open-ended `{n,}`
is a ReDoS surface (the original `jwt` pattern was quadratic on `eyJ…` input).

The package is stdlib-only and import-linter-enforced (its own contract plus
membership in the shared-libraries contract): it runs inside the logging
pipeline of every process, so nothing that logs may be imported from it.

## The sinks

| Surface | Where | Mechanism |
|---|---|---|
| (a) gateway | `LlmGateway.chat/stream/embed` | request DTOs redacted before ANY adapter — covers all providers, fallback chains, `/api/v1/llm/*`, and the memory embedder's provider calls |
| (b) logs | `observability/logging.py` `_redact_event` | in `ProcessorFormatter.processors` before `JSONRenderer` — the only point where structlog-native AND foreign stdlib records converge, app-side of promtail |
| (c) sandbox stream | `ToolOutputStream.emit` | line-buffered redaction of `tool.output` chunks (a token split across docker frames reassembles before the redactor); the bus flushes the tail when the call ends |
| tool envelopes | `tools/redaction.default_redactor` | string path now delegates to the shared engine → inventory+entropy reach results/stderr/artifacts/errors and approval bodies automatically |

All default paths read the process-global engine
(`titan.redaction.runtime.default_engine()`): logging is process-global and
configured before the vault exists, so the composition roots enrich the
singleton in place (inventory, reporter) instead of injecting instances. A
bare `LlmGateway(...)` with no wiring still redacts patterns+entropy — the
control fails closed (the Phase-68 lesson).

Failure modes are closed at each sink: the log processor replaces the whole
line with `log.redaction_failed` if the engine raises; the stream publishes
`[REDACTION-ERROR]` instead of the raw chunk; a crashing reporter never
blocks redaction.

## SEC-055 — secret-bearing file exclusion

`titan.redaction.paths.is_secret_path` (Vol 8 patterns verbatim, plus the
ssh-key family, `.aws/`, `.config/gcloud/`, `.azure/`, `.kube/config`,
`.docker/config.json`, `.netrc`, `.pgpass`, `.npmrc`, `.pypirc`,
`.git-credentials`; case- and separator-insensitive) is enforced at every
file-READ surface: `fs.read` (PERMISSION_DENIED), `fs.list`/`fs.glob` (omit),
`fs.grep` (skip), `code.test`'s report reader, and the plugin `fs.read`
capability (deny-above-grant). Writes stay allowed — generated apps
legitimately write `.env` files; they just can't be read back into context.

Two honest limitations, by design:

- **`terminal.run` cannot be filename-filtered** — `cat .env` returns bytes,
  not paths. That surface is covered by CONTENT redaction (the bus envelope +
  the output stream), which the inventory detector makes meaningful: a
  vaulted value read out of a file IS redacted even with no token shape. A
  never-vaulted, low-entropy, unshaped value (`hunter2` alone in a .env an
  agent cats) is not catchable by any content mechanism — the filename block
  at the fs layer plus vaulting real secrets is the defense.
- **RAG ingestion has no file path to filter.** Exclusion is enforced where
  files actually enter the system (the tool layer); the ingestion DoD test
  drives the REAL relay→embedder→Qdrant pipeline and proves nothing
  secret-bearing reaches the embeddings provider (sink (a) redacts the
  embed input) or the Qdrant store.

## Audit + refresh

Hits are counted (deduplicated by sink/class/hash8, bounded, overflow-safe)
in a `RedactionReporter` and flushed to `secret_audit` as
`redaction.hit` rows (`detail = {sink, class, hash8, count}`) by a
maintenance loop in BOTH processes (`vault/inventory.run_redaction_maintenance`,
30s), which also refreshes the inventory digests. Vault writes additionally
refresh inline in the API process. A refresh failure keeps the previous
index (degradation is stale-but-active, never off); a flush failure re-queues
the drained records. The `keygen` CLI (`python -m titan.vault keygen`) still
prints the age identity to raw stdout by design — it is an operator utility,
never a scraped container process.

## Accepted residuals (Phase-69 security review)

These were surfaced by the review and deliberately NOT changed; they are
defense-in-depth gaps, not attacker-reachable leaks:

- **Non-vaulted, pattern-less secrets that are pure-hex or all-letters pass.**
  The entropy guards (`entropy.py`) skip pure-hex (trace ids, digests) and
  digit-less tokens (English identifiers) to keep logs usable. A secret that
  is (a) not vaulted, (b) not a known provider shape, and (c) hex or
  all-letters is not caught. Vault real secrets (then the inventory detector
  catches them regardless of shape).
- **The CLI and egress-proxy processes redact patterns+entropy but not
  inventory.** Only the API and worker roots hold the vault backend and run
  `run_redaction_maintenance`. The CLI is operator-local and short-lived; the
  egress proxy logs request metadata, not credential-bearing bodies.
- **Cold-boot / first-refresh window.** The process-global engine starts with
  patterns+entropy live and an EMPTY inventory; the first inventory refresh is
  scheduled before the gateway serves traffic. Startup log lines in that window
  redact against patterns+entropy only. No startup path logs a vault value.
- **The hash8 in Loki/audit is a weak offline oracle for LOW-entropy secrets**
  (32-bit unsalted sha256). Both sinks are access-controlled (`logs:read` and
  `secret_audit` are SENSITIVE_RESOURCES). The third-party-visible half (the
  gateway marker) carries no hash at all.
