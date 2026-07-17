/**
 * The message kinds that actually exist (Vol 4 §6.6's "typed chip").
 *
 * §6.6 names eight "Volume 2 message types": TASK_ASSIGNMENT, PROGRESS_REPORT,
 * REVIEW_REQUEST, BUG_REPORT, DECISION, BLOCKER, MEETING_SUMMARY,
 * CONFLICT_RESOLUTION. That list is wrong about its own source, in two ways
 * worth stating because a chip set built from it would misrepresent the data:
 *
 * 1. **BLOCKER is not a message type.** Vol 2's eight are TaskAssignment,
 *    ProgressReport, ReviewRequest, **ArchitectureDiscussion**, BugReport,
 *    DecisionLog, MeetingSummary, ConflictResolution. A blocker is a FIELD —
 *    `ProgressReportPayload.blockers[]`, alongside `status: "blocked"`. §6.6
 *    dropped ArchitectureDiscussion and promoted a field to a type.
 * 2. **There is a ninth kind.** `human` is in the backend's `MessageKind`
 *    Literal and is how a person posts. §6.6 calls this "posts as role
 *    `client`"; there is no `client` role (roles are owner/admin/maintainer/
 *    operator/viewer) — the human-ness is the message's KIND, not the sender's
 *    role.
 *
 * The names below are the wire values: the DB column is free text with no CHECK
 * constraint, and `MessageCreate.kind` is the only thing that constrains them.
 */

/** The backend's `MessageKind` Literal, verbatim. */
export const MESSAGE_KINDS = [
  "task_assignment",
  "progress_report",
  "review_request",
  "architecture_discussion",
  "bug_report",
  "decision_log",
  "meeting_summary",
  "conflict_resolution",
  "human",
] as const;

export type MessageKind = (typeof MESSAGE_KINDS)[number];

export type ChipVariant = "default" | "secondary" | "outline" | "destructive";

interface KindMeta {
  readonly label: string;
  readonly variant: ChipVariant;
}

const KINDS: Record<MessageKind, KindMeta> = {
  task_assignment: { label: "assignment", variant: "default" },
  progress_report: { label: "progress", variant: "secondary" },
  review_request: { label: "review", variant: "outline" },
  architecture_discussion: { label: "architecture", variant: "outline" },
  bug_report: { label: "bug", variant: "destructive" },
  decision_log: { label: "decision", variant: "default" },
  meeting_summary: { label: "meeting", variant: "secondary" },
  conflict_resolution: { label: "conflict", variant: "destructive" },
  human: { label: "human", variant: "default" },
};

export function isMessageKind(kind: string): kind is MessageKind {
  return (MESSAGE_KINDS as readonly string[]).includes(kind);
}

/**
 * The chip for a kind.
 *
 * Falls back rather than throwing on an unknown value: `messages.kind` is a
 * free-text column with no CHECK constraint, so a row written outside
 * `MessageCreate` can hold anything, and a feed must render it rather than
 * crash.
 */
export function kindMeta(kind: string): KindMeta {
  return isMessageKind(kind)
    ? KINDS[kind]
    : { label: kind, variant: "outline" };
}

/**
 * A blocked progress report, which is as close as this system gets to §6.6's
 * "BLOCKER" chip.
 *
 * Reads the body defensively: every REST message body is validated by
 * `_permissive` (Vol 2's strict per-kind schemas are registered for the agent
 * bus, not for the API), so `body` is untrusted free JSON and `status` may be
 * anything or absent.
 */
export function isBlocked(kind: string, body: unknown): boolean {
  if (kind !== "progress_report" || typeof body !== "object" || body === null) {
    return false;
  }
  const record = body as Record<string, unknown>;
  if (record.status === "blocked") {
    return true;
  }
  return Array.isArray(record.blockers) && record.blockers.length > 0;
}
