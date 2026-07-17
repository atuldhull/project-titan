import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import type { MessageOut } from "@titan/api-client";

import { MessageRow } from "./message-row";
import { StreamBubble } from "./stream-bubble";

/**
 * The §6.6 message row across every kind that exists (TITAN-FRT-002).
 *
 * Stories cover the ROW and the streaming BUBBLE, not the screen: the screen's
 * four states are query states behind a QueryClient and a socket.
 *
 * These double as the visible record of TITAN-FRT-029's real reach — the task
 * chips are links; the PR, story and decision chips are inert, because there is
 * nowhere to send anyone. Hover one to see why.
 */
const meta: Meta<typeof MessageRow> = {
  title: "Conversations/MessageRow",
  component: MessageRow,
};
export default meta;
type Story = StoryObj<typeof MessageRow>;

const PID = "11111111-1111-7111-8111-111111111111";
const TASK = "22222222-2222-7222-8222-222222222222";
const DECISION = "33333333-3333-7333-8333-333333333333";

function message(over: Partial<MessageOut>): MessageOut {
  return {
    id: "44444444-4444-7444-8444-444444444444",
    project_id: PID,
    thread_id: "55555555-5555-7555-8555-555555555555",
    kind: "progress_report",
    sender_agent_id: null,
    sender_user_id: null,
    from_agent: "BE-2",
    to_agent: null,
    body: {},
    task_id: null,
    schema_version: "1.0",
    correlation_id: null,
    priority: "normal",
    requires_ack: false,
    created_at: "2026-07-01T12:02:00Z",
    version: 1,
    ...over,
  } as MessageOut;
}

export const TaskAssignment: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "task_assignment",
      from_agent: "PM-1",
      task_id: TASK,
      body: { title: "Implement POST /api/v1/orders", depends_on: [DECISION] },
    }),
  },
};

/**
 * §6.6 wants a BLOCKER chip. There is no blocker message type — Vol 2's eighth
 * is ArchitectureDiscussion. A blocker is a progress_report with status
 * "blocked", which is what this shows.
 */
export const ProgressBlocked: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "progress_report",
      task_id: TASK,
      body: {
        status: "blocked",
        summary: "T-231 blocked: payment sandbox credentials",
        blockers: [{ description: "no creds", needs: "devops" }],
      },
    }),
  },
};

/** The PR chip is inert: `artifact_ref` is a free string and there is no PR route. */
export const ReviewRequest: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "review_request",
      from_agent: "Reviewer",
      requires_ack: true,
      body: {
        artifact_ref: "pr:42",
        context_task_id: TASK,
        summary: "Orders endpoint",
      },
    }),
  },
};

export const BugReport: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "bug_report",
      from_agent: "QA-1",
      body: { severity: "high", summary: "Order total ignores discounts" },
    }),
  },
};

export const DecisionLog: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "decision_log",
      from_agent: "CEO",
      body: { decision: "Approve the mock payment gateway for the MVP" },
    }),
  },
};

/** Vol 2's real eighth type, which §6.6's list drops entirely. */
export const ArchitectureDiscussion: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "architecture_discussion",
      from_agent: "Architect",
      body: { question: "Sync or async order-payment integration?" },
    }),
  },
};

/**
 * The decision chips are inert on purpose: `meeting_summary.decisions[]` holds
 * decision_log MESSAGE uuids, which are not rows in the `decisions` table the
 * ADR panel renders. Linking them would assert an identity the data lacks.
 */
export const MeetingSummary: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "meeting_summary",
      from_agent: "PM-1",
      body: {
        summary: "Sprint 3 scoped to checkout flow",
        decisions: [DECISION],
      },
    }),
  },
};

export const ConflictResolution: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "conflict_resolution",
      from_agent: "QA-1",
      body: { subject: "Is BUG-77 release-blocking?", conflict_id: DECISION },
    }),
  },
};

/** The ninth kind: how a person posts. §6.6 calls this "role client"; there is
 *  no such role — it is the message's kind. */
export const Human: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "human",
      from_agent: null,
      sender_user_id: "66666666-6666-7666-8666-666666666666",
      body: { text: "Ship the MVP without the discount engine." },
    }),
  },
};

/** `messages.kind` is free text with no CHECK constraint — the feed renders an
 *  unknown value rather than crashing. */
export const UnknownKind: Story = {
  args: {
    projectId: PID,
    message: message({
      kind: "something_new",
      body: { summary: "From a newer producer" },
    }),
  },
};

/**
 * A token stream. The caret blinks only under `motion-safe` (TITAN-FRT-045);
 * with prefers-reduced-motion on, the text still streams and the caret sits
 * still. Attributed to a RUN and its agent — the frame carries no thread or
 * message id, so it cannot belong to a bubble.
 */
export const Streaming: StoryObj<typeof StreamBubble> = {
  render: () => (
    <StreamBubble
      stream={{
        runId: "r1",
        agentId: "77777777-7777-7777-8777-777777777777",
        text: "Weighing async via the outbox…",
        done: false,
      }}
    />
  ),
};
