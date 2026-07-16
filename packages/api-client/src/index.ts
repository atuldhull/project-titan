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
