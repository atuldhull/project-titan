/**
 * The message feed's decisions, kept pure.
 *
 * The feed itself is a virtualizer over a scroll container: jsdom has no
 * layout, so `getBoundingClientRect` is all zeros and the rendered list can
 * never be honestly exercised in this repo's test environment. Everything that
 * can be decided without pixels is therefore decided here — ordering, dedup,
 * the at-bottom rule, the unread count — and the component is left with as
 * little judgement as possible.
 */

import type { MessageOut } from "@titan/api-client";

/**
 * Whether this screen is currently covered by a modal.
 *
 * A screen-scoped shortcut on a `window` listener still hears keys aimed at an
 * open dialog, sheet or dropdown — Phase 60 shipped exactly that bug — so a
 * handler that navigates MUST first ask whether the page is really the user's
 * to drive.
 *
 * The test is the element's OWN state rather than a hunt for someone else's
 * markup: Radix (and the `aria-modal` pattern generally) marks the rest of the
 * page `aria-hidden` while a modal is open. So if our own container sits inside
 * an aria-hidden subtree, we are behind something and these keys are not ours.
 * That is a documented, library-agnostic contract — unlike grepping for
 * `[data-radix-*]`, which would break on an upgrade exactly the way the SVG
 * export did.
 */
export function isBehindModal(element: Element | null): boolean {
  return element?.closest('[aria-hidden="true"]') != null;
}

/**
 * How close to the bottom still counts as "at the bottom".
 *
 * Not zero: sub-pixel rounding and the scrollbar's own rounding mean an
 * honestly-pinned view often sits a pixel or two short, and a strict equality
 * would flip the pill on for a user who never moved.
 */
export const AT_BOTTOM_THRESHOLD_PX = 48;

export interface ScrollPosition {
  readonly scrollTop: number;
  readonly scrollHeight: number;
  readonly clientHeight: number;
}

/** §6.6: auto-scroll ONLY when the reader is already at the bottom. */
export function isAtBottom(position: ScrollPosition): boolean {
  const distance =
    position.scrollHeight - position.scrollTop - position.clientHeight;
  return distance <= AT_BOTTOM_THRESHOLD_PX;
}

/**
 * Flatten cursor pages into one oldest-first list.
 *
 * The API sorts newest-first (`-created_at`), which is what pagination needs —
 * page 2 is OLDER than page 1 — but the reverse of what a chat reads like. The
 * flip happens once, here.
 *
 * Deduped by id: at-least-once delivery plus a refetch racing a `message.created`
 * frame can genuinely deliver the same row twice, and a duplicated React key is
 * a silent render bug.
 */
export function orderOldestFirst(
  pages: readonly { items: MessageOut[] }[],
): MessageOut[] {
  const byId = new Map<string, MessageOut>();
  for (const page of pages) {
    for (const message of page.items) {
      byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort((a, b) => {
    const byTime = a.created_at.localeCompare(b.created_at);
    // created_at can tie; ids are UUIDv7, so they break it in real time order.
    return byTime !== 0 ? byTime : a.id.localeCompare(b.id);
  });
}

/**
 * Where to restore the scroll after OLDER messages are prepended.
 *
 * The classic reverse-infinite-scroll bug: loading history grows the content
 * ABOVE the viewport, so a fixed `scrollTop` now points somewhere else entirely
 * and the reader is thrown away from the message they were reading — usually
 * straight back to the top, which immediately triggers another page load.
 *
 * Anchoring on the distance from the BOTTOM fixes it: that distance is
 * unchanged by anything prepended above, so restoring it puts the same message
 * back under the reader's eyes.
 */
export function scrollTopAfterPrepend(
  before: { scrollTop: number; scrollHeight: number },
  afterScrollHeight: number,
): number {
  const grew = afterScrollHeight - before.scrollHeight;
  return grew > 0 ? before.scrollTop + grew : before.scrollTop;
}

/** §6.6's message-type filter. */
export function filterByKind(
  messages: readonly MessageOut[],
  kinds: ReadonlySet<string>,
): MessageOut[] {
  return kinds.size === 0
    ? [...messages]
    : messages.filter((m) => kinds.has(m.kind));
}

/**
 * One in-flight token stream (§6.6's "▍streaming reply…").
 *
 * Keyed by RUN, not by message or thread — which is the honest shape of the
 * only streaming frame that exists. `run.output.delta` carries `{run_id, delta,
 * agent_id?}` and nothing else: there is no thread_id and no message id on it,
 * so a stream CANNOT be attached to a particular message bubble. It can only be
 * attributed to the run (and its agent) inside the project's channel, which is
 * exactly what this models.
 *
 * (§6.6 asks for `message.chunk`. No such frame exists anywhere in the backend.
 * `run.output.delta` is the only token stream, and today its only producer is
 * the CEO intake flow, gated behind `ceo_intake_enabled`, default off.)
 */
export interface Stream {
  readonly runId: string;
  readonly agentId: string | null;
  readonly text: string;
  readonly done: boolean;
}

export type Streams = ReadonlyMap<string, Stream>;

export type StreamEvent =
  | {
      readonly type: "delta";
      readonly runId: string;
      readonly agentId: string | null;
      readonly delta: string;
    }
  | { readonly type: "finished"; readonly runId: string };

/**
 * Fold token frames into per-run buffers.
 *
 * A reducer rather than component state so the ordering rules are testable: in
 * particular that a `run.finished` arriving BEFORE some of its deltas (the
 * frames are independent, and delta is unbuffered while lifecycle is buffered)
 * does not truncate the text.
 */
export function reduceStreams(streams: Streams, event: StreamEvent): Streams {
  const next = new Map(streams);
  if (event.type === "finished") {
    const existing = next.get(event.runId);
    if (!existing) {
      // Nothing streamed for this run: not every run emits deltas (almost none
      // do). Recording an empty finished stream would render an empty bubble.
      return streams;
    }
    next.set(event.runId, { ...existing, done: true });
    return next;
  }
  const existing = next.get(event.runId);
  next.set(event.runId, {
    runId: event.runId,
    // A later delta may carry the agent id when an earlier one did not.
    agentId: event.agentId ?? existing?.agentId ?? null,
    text: (existing?.text ?? "") + event.delta,
    // A delta after `finished` means the stream was not over; unset `done`
    // rather than drop the token on the floor.
    done: false,
  });
  return next;
}

/** The streams still worth rendering: anything unfinished with text. */
export function liveStreams(streams: Streams): Stream[] {
  return [...streams.values()].filter((s) => !s.done && s.text !== "");
}

/**
 * Drop finished streams.
 *
 * Called when the message log grows: an agent's completed reply arrives as an
 * ordinary `message.created` row, and keeping the finished stream around too
 * would leave its bubble sitting under the real message forever. Without this
 * the map only ever grows — every completed intake turn in a long session stays
 * in memory for the life of the tab.
 */
export function pruneStreams(streams: Streams): Streams {
  const next = new Map<string, Stream>();
  for (const [runId, stream] of streams) {
    if (!stream.done) {
      next.set(runId, stream);
    }
  }
  return next;
}
