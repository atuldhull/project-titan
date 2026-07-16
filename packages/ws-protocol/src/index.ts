/**
 * @titan/ws-protocol — shared realtime envelope, topic and event types for the
 * single multiplexed browser socket (Vol 4 §5, ADR-FRT-03).
 *
 * This package is the ONE authoritative TypeScript description of what the
 * FastAPI `/ws` endpoint actually speaks. The backend wire envelope is
 * `{ v, type, seq, ts, project_id, payload }` — the discriminator is `type`
 * (NOT a `topic` field), and subscriptions are addressed by
 * `(project_id, category)` plus three always-on channels. The Vol 4 §5.2
 * "8 core topics" are the conceptual model layered on top of that wire format;
 * `CORE_TOPICS` records the mapping so the client can subscribe and reconcile
 * without duplicating knowledge of the protocol.
 *
 * Pure types + tiny pure helpers only — no React, no DOM, no fetch. Safe to
 * import from server components, route handlers, workers, and tests alike.
 */

/** `v` on every server frame — bump only on a breaking envelope change. */
export const PROTOCOL_VERSION = 1 as const;

// --- Server -> client -------------------------------------------------------

/** Domain events the server pushes (realtime.protocol + realtime.fanout). */
export const DOMAIN_FRAME_TYPES = [
  "task.created",
  "task.updated",
  "task.kanban",
  "run.started",
  "run.finished",
  "run.output.delta",
  "message.created",
  "decision.recorded",
  "agent.status",
  "sprint.updated",
  "pipeline.updated",
  "handoff.evaluated",
  "clarification.updated",
  "sys.notice",
  "notification.created",
  "activity.created",
] as const;
export type DomainFrameType = (typeof DOMAIN_FRAME_TYPES)[number];

/** Control frames the server emits in response to client ops / lifecycle. */
export const CONTROL_FRAME_TYPES = [
  "subscribed",
  "unsubscribed",
  "ping",
  "pong",
  "error",
  "sys.resync",
  "authenticated",
] as const;
export type ControlFrameType = (typeof CONTROL_FRAME_TYPES)[number];

export type ServerFrameType = DomainFrameType | ControlFrameType;

/**
 * The exact server->client envelope. Field order and names mirror the backend
 * `protocol.build_frame`; `seq` is monotonic per delivery channel (per-project
 * for project frames, `0` for control/user/agent frames), and `project_id` is
 * null for user/agent/org frames.
 */
export interface ServerFrame<P = unknown> {
  readonly v: typeof PROTOCOL_VERSION;
  readonly type: ServerFrameType;
  readonly seq: number;
  readonly ts: string;
  readonly project_id: string | null;
  readonly payload: P;
}

/** `error` control-frame payload: `{ code, message }`. */
export interface ErrorFramePayload {
  readonly code: string;
  readonly message?: string;
}

/** `subscribed` / `unsubscribed` ack payload. */
export interface SubscriptionAckPayload {
  readonly project_id: string | null;
  readonly topics: readonly string[];
}

// --- Client -> server -------------------------------------------------------

/** Subscribe to a project's categories (or the shell ack when project is null). */
export interface SubscribeFrame {
  readonly type: "subscribe";
  readonly project_id: string | null;
  readonly topics: readonly string[];
  readonly last_seq?: number;
}

export interface UnsubscribeFrame {
  readonly type: "unsubscribe";
  readonly project_id: string;
  readonly topics: readonly string[];
}

export interface PingFrame {
  readonly type: "ping";
}

export interface PongFrame {
  readonly type: "pong";
}

export interface AuthenticateFrame {
  readonly type: "authenticate";
  readonly token: string;
}

export type ClientFrame =
  | SubscribeFrame
  | UnsubscribeFrame
  | PingFrame
  | PongFrame
  | AuthenticateFrame;

// --- Topics -----------------------------------------------------------------

/**
 * Per-project subscription categories (backend `ConnectionManager._category`).
 * A `subscribe` with `topics: []` receives every category for the project.
 */
export const PROJECT_TOPIC_CATEGORIES = [
  "tasks",
  "runs",
  "messages",
  "decisions",
  "sprints",
  "sys",
  "pipeline",
  "handoff",
  "clarification",
] as const;
export type ProjectTopicCategory = (typeof PROJECT_TOPIC_CATEGORIES)[number];

/**
 * Always-on channels: auto-delivered to every one of a user's sockets. The bell
 * and agent badges need no explicit subscribe; the org feed does (and requires
 * org-wide `project:read`).
 */
export const ALWAYS_ON_TOPICS = {
  notifications: "notifications:me",
  agents: "agents:status",
  orgActivity: "org:activity",
} as const;

/**
 * The Vol 4 §5.2 conceptual "8 core topics", mapped onto the real wire model so
 * a screen can declare the topic it needs and the client resolves the concrete
 * `(project_id, category)` subscription + the frame types to reconcile.
 *
 *   - `scope: "project"` topics require a `subscribe` carrying that project id
 *     and the listed categories.
 *   - `scope: "always-on"` topics arrive automatically once connected
 *     (`org` still needs an explicit subscribe to `org:activity`).
 */
export interface CoreTopic {
  readonly id: string;
  readonly scope: "project" | "always-on" | "org";
  /** Backend categories to include in `subscribe.topics` (project scope). */
  readonly categories: readonly ProjectTopicCategory[];
  /** Server frame `type`s delivered on this topic. */
  readonly events: readonly DomainFrameType[];
}

export const CORE_TOPICS = {
  orgActivity: {
    id: "org:activity",
    scope: "org",
    categories: [],
    events: ["activity.created"],
  },
  projectTasks: {
    id: "project:tasks",
    scope: "project",
    categories: ["tasks"],
    events: ["task.created", "task.updated", "task.kanban"],
  },
  agentsStatus: {
    id: "agents:status",
    scope: "always-on",
    categories: [],
    events: ["agent.status"],
  },
  channelMessages: {
    id: "channel:messages",
    scope: "project",
    categories: ["messages"],
    events: ["message.created"],
  },
  projectRuns: {
    id: "project:runs",
    scope: "project",
    categories: ["runs"],
    events: ["run.started", "run.finished", "run.output.delta"],
  },
  projectDeployments: {
    id: "project:deployments",
    scope: "project",
    categories: ["pipeline"],
    events: ["pipeline.updated"],
  },
  monitoringSummary: {
    id: "monitoring:summary",
    scope: "always-on",
    categories: [],
    events: ["agent.status"],
  },
  notifications: {
    id: "notifications:me",
    scope: "always-on",
    categories: [],
    events: ["notification.created"],
  },
} as const satisfies Record<string, CoreTopic>;

export type CoreTopicKey = keyof typeof CORE_TOPICS;

// --- Guards & pure helpers --------------------------------------------------

const DOMAIN_SET: ReadonlySet<string> = new Set(DOMAIN_FRAME_TYPES);
const CONTROL_SET: ReadonlySet<string> = new Set(CONTROL_FRAME_TYPES);

export function isDomainFrame(
  frame: ServerFrame,
): frame is ServerFrame & { type: DomainFrameType } {
  return DOMAIN_SET.has(frame.type);
}

export function isControlFrame(
  frame: ServerFrame,
): frame is ServerFrame & { type: ControlFrameType } {
  return CONTROL_SET.has(frame.type);
}

/**
 * Parse a raw text frame off the socket into a validated `ServerFrame`, or
 * `null` if it is not a well-formed frame of a known type. Never throws — a
 * malformed frame must not take down the socket loop.
 */
export function parseServerFrame(raw: string): ServerFrame | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const f = value as Record<string, unknown>;
  if (
    f.v !== PROTOCOL_VERSION ||
    typeof f.type !== "string" ||
    typeof f.seq !== "number" ||
    typeof f.ts !== "string" ||
    !(DOMAIN_SET.has(f.type) || CONTROL_SET.has(f.type))
  ) {
    return null;
  }
  const projectId = f.project_id;
  if (projectId !== null && typeof projectId !== "string") {
    return null;
  }
  return {
    v: PROTOCOL_VERSION,
    type: f.type as ServerFrameType,
    seq: f.seq,
    ts: f.ts,
    project_id: projectId,
    payload: f.payload,
  };
}

/** Serialize a client frame for `socket.send`. */
export function encodeClientFrame(frame: ClientFrame): string {
  return JSON.stringify(frame);
}

export function subscribeFrame(
  projectId: string | null,
  topics: readonly string[],
  lastSeq?: number,
): SubscribeFrame {
  return lastSeq === undefined
    ? { type: "subscribe", project_id: projectId, topics }
    : { type: "subscribe", project_id: projectId, topics, last_seq: lastSeq };
}

export function unsubscribeFrame(
  projectId: string,
  topics: readonly string[],
): UnsubscribeFrame {
  return { type: "unsubscribe", project_id: projectId, topics };
}

export const PONG_FRAME: PongFrame = { type: "pong" };
export const PING_FRAME: PingFrame = { type: "ping" };

export function authenticateFrame(token: string): AuthenticateFrame {
  return { type: "authenticate", token };
}
