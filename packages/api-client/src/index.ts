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
