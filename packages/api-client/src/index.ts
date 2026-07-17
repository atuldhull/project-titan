/**
 * @titan/api-client — the typed TITAN REST surface for the dashboard
 * (TITAN-FRT-014, TITAN-FRT-016). Re-exports the generated contract types plus
 * the client factory and error type. Regenerate the underlying types with
 * `make generate-types`; the CI drift gate fails on any uncommitted change.
 */

import type { components } from "@titan/schemas";

export {
  ApiError,
  createTitanClient,
  unwrap,
  type Problem,
  type TitanClient,
  type TitanClientOptions,
} from "./client";

/** Convenience aliases for the DTOs the data layer touches most. */
type Schemas = components["schemas"];

export type NotificationOut = Schemas["NotificationOut"];
export type MarkAllReadResult = Schemas["MarkAllReadResult"];
export type MeResponse = Schemas["MeResponse"];
export type TokenPair = Schemas["TokenPair"];
export type PasswordGrant = Schemas["PasswordGrant"];
export type TaskOut = Schemas["TaskOut"];
export type ProjectOut = Schemas["ProjectOut"];
export type NotificationPage = Schemas["Page_NotificationOut_"];
export type TaskPage = Schemas["Page_TaskOut_"];
export type ProjectPage = Schemas["Page_ProjectOut_"];

/** The RUNTIME agent registry row. Note `current_task_id` is always null (the
 *  column has no writer) and `status` only ever holds idle|paused — what an
 *  agent is actually doing lives in {@link RunOut}. */
export type AgentOut = Schemas["AgentOut"];
export type AgentPage = Schemas["Page_AgentOut_"];
/** The DECLARATIVE role contract from the YAML roster — tier, supervisor, KPIs.
 *  Keyed by `agent_id`, which is a DIFFERENT vocabulary from `AgentOut.role`;
 *  join them through `AgentOut.roster_id`. */
export type AgentDefinition = Schemas["AgentDefinition"];
/** The only record of what an agent actually did: its task, the model and
 *  provider really used, token counts, timings, and the error text. */
export type RunOut = Schemas["RunOut"];
export type RunPage = Schemas["Page_RunOut_"];
export type MessageOut = Schemas["MessageOut"];
export type MessagePage = Schemas["Page_MessageOut_"];
/** One structured log line out of Loki. `ts_ns` is a STRING — a Unix
 *  nanosecond exceeds 2**53, so `Number(ts_ns)` silently rounds it and the id,
 *  the cursor and the ordering drift with it. `fields` is the caller's own
 *  kwargs (Vol 9 §6.3's `attrs`, which structlog actually flattens and the
 *  backend reconstructs by subtraction). A non-JSON line — postgres and redis
 *  ship plenty through the same promtail — carries only `line` and `labels`. */
export type LogLine = Schemas["LogLine"];
export type LogPage = Schemas["Page_LogLine_"];

/** Aggregate LLM cost/tokens over a range (Vol 4 §6.10, TITAN-FRT-033).
 *  `cost_usd` is a STRING everywhere in here: it is `Numeric(14,6)`, and the
 *  totals are asserted to the micro-dollar against the ledger, which a float
 *  cannot carry. Convert only at the point of drawing a pixel. */
export type UsageSummary = Schemas["UsageSummary"];
export type UsageGroup = Schemas["UsageGroup"];
export type UsagePoint = Schemas["UsagePoint"];

/** One memory record as the Explorer's browse list shows it. `record_subtype`
 *  is what §6.11's tier tree calls the "Patterns"/"Lessons" tiers — they are
 *  not tiers, they are `metadata->>'record_subtype'` on an ordinary record. */
export type MemoryBrowseRow = Schemas["MemoryBrowseRow"];
export type MemoryBrowsePage = Schemas["Page_MemoryBrowseRow_"];
export type MemoryFacets = Schemas["MemoryFacets"];
/** `score` is Qdrant's cosine similarity — NOT the §6.1 pipeline's blended,
 *  set-relative `final_score`. That is what makes §6.11's ≥0.6 floor mean
 *  something. */
export type MemorySearchResult = Schemas["MemorySearchResult"];
export type MemorySearchHit = Schemas["MemorySearchHit"];
/** A record with its chunks and its COMPLETE ledger trail — the provenance
 *  chain and usage history TITAN-FRT-034 requires. */
export type MemoryRecordDetail = Schemas["MemoryRecordDetail"];
export type DeploymentOut = Schemas["DeploymentOut"];
