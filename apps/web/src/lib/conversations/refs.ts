/**
 * What a message references, and where (if anywhere) that can be opened —
 * TITAN-FRT-029.
 *
 * FRT-029 (MUST) says "every structured message SHALL deep-link to its
 * referenced entity (task, PR, ADR, deployment)". Held against the real schema,
 * one of those four is a link and the rest are not:
 *
 * - **task** — REAL. `messages.task_id` is a typed, FK'd column, and
 *   `review_request.context_task_id` / `task_assignment.depends_on[]` carry more
 *   task uuids. `/projects/{pid}/board?task=` opens them.
 * - **PR** — NOT LINKABLE. The reference exists as a STRING SCHEME inside
 *   `review_request.artifact_ref` ("pr:42"), which nothing validates, and the
 *   app has no pull-request route to send anyone to. The number is real; the
 *   destination is not.
 * - **ADR** — NOT LINKABLE, and the trap here is subtle. A `decision_log`
 *   MESSAGE is not a row in the `decisions` table; they are separate records
 *   written by separate endpoints. Linking a decision_log message to the
 *   architecture screen's ADR panel would assert an identity the data does not
 *   have. (`meeting_summary.decisions[]` holds MESSAGE uuids, not decision ids —
 *   the same trap, spelled differently.)
 * - **deployment** — NO REFERENCE EXISTS. Not one payload in Vol 2 §6.2 carries
 *   a deployment id, and none of the nine kinds has a field for one.
 *
 * So a ref is rendered either as a link (when a route exists) or as an inert,
 * labelled reference. A chip that looks clickable and goes nowhere is worse
 * than one that plainly says "pr:42".
 *
 * Everything here parses DEFENSIVELY. Every REST message body goes through
 * `_permissive` — the strict Vol 2 schemas are registered for the agent bus,
 * not the API — so `body` is untrusted free JSON of any shape.
 */

import type { MessageOut } from "@titan/api-client";

export type RefKind =
  "task" | "story" | "pr" | "artifact" | "message" | "conflict" | "other";

export interface MessageRef {
  readonly kind: RefKind;
  /** What to show. */
  readonly label: string;
  /** Where to go, or null when the entity is named but unreachable. */
  readonly href: string | null;
  /** Why there is no link — surfaced as a tooltip so the gap is legible. */
  readonly reason?: string;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function short(id: string): string {
  return id.slice(0, 8);
}

function taskRef(taskId: string, projectId: string, label: string): MessageRef {
  return {
    kind: "task",
    label,
    href: `/projects/${projectId}/board?task=${taskId}`,
  };
}

function asRecord(body: unknown): Record<string, unknown> {
  return typeof body === "object" && body !== null
    ? (body as Record<string, unknown>)
    : {};
}

/**
 * Parse an `artifact_ref` string.
 *
 * Vol 2's example is `"pr:42"`, alongside `artifact_type: "pull_request | plan |
 * architecture | test_plan | release"`. It is a plain `str` — the scheme is a
 * convention, not a validated format — so this recognises it without depending
 * on it.
 */
export function parseArtifactRef(raw: string): MessageRef {
  const [scheme, ...rest] = raw.split(":");
  const value = rest.join(":");
  if (scheme === "pr" && value !== "") {
    return {
      kind: "pr",
      label: `PR #${value}`,
      href: null,
      reason: "This app has no pull-request screen to open.",
    };
  }
  return {
    kind: "artifact",
    label: raw,
    href: null,
    reason: "Not a linkable entity.",
  };
}

/**
 * Every entity a message names, in the order a reader wants them.
 *
 * `projectId` is required for task links — a task route is project-scoped, and
 * the message carries the project it belongs to.
 */
export function refsOf(message: MessageOut, projectId: string): MessageRef[] {
  const refs: MessageRef[] = [];
  const body = asRecord(message.body);

  // The one typed, FK'd reference in the whole schema.
  if (message.task_id != null) {
    refs.push(
      taskRef(message.task_id, projectId, `Task ${short(message.task_id)}`),
    );
  }

  // review_request: the task under review, and the artifact it is about.
  if (
    isUuid(body.context_task_id) &&
    body.context_task_id !== message.task_id
  ) {
    refs.push(
      taskRef(
        body.context_task_id,
        projectId,
        `Task ${short(body.context_task_id)}`,
      ),
    );
  }
  if (typeof body.artifact_ref === "string" && body.artifact_ref !== "") {
    refs.push(parseArtifactRef(body.artifact_ref));
  }

  // task_assignment: the story it belongs to and the tasks it waits on.
  if (isUuid(body.story_id)) {
    refs.push({
      kind: "story",
      label: `Story ${short(body.story_id)}`,
      href: null,
      reason: "There is no story screen.",
    });
  }
  if (Array.isArray(body.depends_on)) {
    for (const dep of body.depends_on) {
      if (isUuid(dep)) {
        refs.push(taskRef(dep, projectId, `Depends on ${short(dep)}`));
      }
    }
  }

  // architecture_discussion: the message it answers.
  if (isUuid(body.responding_to)) {
    refs.push({
      kind: "message",
      label: `Replying to ${short(body.responding_to)}`,
      href: null,
      reason: "Messages have no permalink route.",
    });
  }

  // meeting_summary.decisions[] holds MESSAGE uuids — not `decisions` rows, so
  // these cannot open the ADR panel.
  if (Array.isArray(body.decisions)) {
    for (const decision of body.decisions) {
      if (isUuid(decision)) {
        refs.push({
          kind: "message",
          label: `Decision ${short(decision)}`,
          href: null,
          reason:
            "This names a decision_log MESSAGE, which is not an ADR record.",
        });
      }
    }
  }

  // conflict_resolution: a real conflicts table, with no screen.
  if (isUuid(body.conflict_id)) {
    refs.push({
      kind: "conflict",
      label: `Conflict ${short(body.conflict_id)}`,
      href: null,
      reason: "There is no conflict screen.",
    });
  }

  return refs;
}

/**
 * The one-line summary under a message.
 *
 * The transition log is LOSSY — many actions collapse onto one kind (start,
 * block, merge and fail all become `progress_report`), so what happened is
 * `body.action`, never `kind`. Prose fields come next, in the order the eight
 * payloads actually name them.
 */
export function summarize(message: MessageOut): string | null {
  const body = asRecord(message.body);
  for (const field of [
    "action",
    "summary",
    "title",
    "text",
    "question",
    "decision",
    "subject",
  ]) {
    const value = body[field];
    if (typeof value === "string" && value.trim() !== "") {
      return value;
    }
  }
  return null;
}
