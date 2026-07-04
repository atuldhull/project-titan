# Redis Streams Message Envelope — Contract

Status: **reserved in Phase 4**; implemented by the events module in Phase 10.
Sources of truth: Vol 3 §7 (TITAN-BCK-010), Vol 9 §6.4 (TITAN-OPS-016).

Every message published to a Redis Stream carries this envelope:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | UUIDv7 | Unique event identity; consumers dedupe on it (`seen:{consumer_group}`, 24 h TTL) |
| `topic` | string | Logical event name, e.g. `tasks.ready` |
| `occurred_at` | RFC 3339 timestamp | When the state change committed |
| `actor` | string | Emitting principal (`api`, agent id, `system`) |
| `payload` | object | Event-specific body (JSON) |
| `trace_id` | 32-hex string | OTel trace id, for log/DB correlation (TITAN-BCK-010) |
| `traceparent` | W3C traceparent string | **Reserved (TITAN-OPS-016):** full trace context, e.g. `00-<trace_id>-<span_id>-01`. Producers inject it from the active span; consumers resume the context on consume so one trace covers API → orchestrator → worker → LLM gateway. Wired when workers land (Phase 10+). |

Rules already binding on every future producer/consumer:

1. Consumers are idempotent keyed on `event_id` — at-least-once delivery is
   assumed everywhere (TITAN-BCK-010).
2. `traceparent` is metadata, never payload: the tracing layer reads it; domain
   handlers must not depend on it.
3. Traces never contain prompt or completion text (TITAN-OPS-016) — the
   envelope carries ids only; bodies live in PostgreSQL run artifacts.
