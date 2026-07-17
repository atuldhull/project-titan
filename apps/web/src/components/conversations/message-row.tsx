"use client";

import Link from "next/link";

import type { MessageOut } from "@titan/api-client";

import { Badge } from "@/components/ui/badge";
import { isBlocked, kindMeta } from "@/lib/conversations/kinds";
import { refsOf, summarize } from "@/lib/conversations/refs";
import { initials } from "@/lib/agents/initials";

/**
 * The §6.6 typed chip plus its references.
 *
 * A ref with an `href` renders as a link; one without renders as an inert
 * badge carrying the reason in its tooltip. That asymmetry is the honest
 * shape of TITAN-FRT-029: of its four named entities (task, PR, ADR,
 * deployment) only a task is reachable — see `lib/conversations/refs`.
 */
function Refs({
  message,
  projectId,
}: {
  message: MessageOut;
  projectId: string;
}) {
  const refs = refsOf(message, projectId);
  if (refs.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {refs.map((ref, i) =>
        ref.href ? (
          <Link
            key={`${ref.label}-${i}`}
            href={ref.href}
            className="text-xs text-primary underline-offset-2 hover:underline"
          >
            {ref.label}
          </Link>
        ) : (
          <Badge
            key={`${ref.label}-${i}`}
            variant="outline"
            className="font-mono text-xs text-muted-foreground"
            title={ref.reason}
          >
            {ref.label}
          </Badge>
        ),
      )}
    </div>
  );
}

function senderLabel(message: MessageOut): string {
  // from_agent is the bus's own display name; sender_agent_id is the FK. A
  // human message has neither and carries sender_user_id instead.
  if (message.from_agent) {
    return message.from_agent;
  }
  if (message.sender_agent_id) {
    return message.sender_agent_id.slice(0, 8);
  }
  return message.sender_user_id ? "You" : "system";
}

export function MessageRow({
  message,
  projectId,
}: {
  message: MessageOut;
  projectId: string;
}) {
  const meta = kindMeta(message.kind);
  const summary = summarize(message);
  const blocked = isBlocked(message.kind, message.body);
  const sender = senderLabel(message);

  return (
    <article className="flex gap-2 px-1 py-2">
      <span
        aria-hidden
        className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-medium"
      >
        {initials(sender)}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-baseline gap-1.5">
          <span className="text-xs font-medium">{sender}</span>
          <time
            className="text-xs text-muted-foreground"
            dateTime={message.created_at}
          >
            {new Date(message.created_at).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </time>
          <Badge variant={meta.variant}>{meta.label}</Badge>
          {/* §6.6 wants a BLOCKER chip. There is no blocker message type — it is
              progress_report with status "blocked" (or a non-empty blockers[]),
              so the chip says what the data says. */}
          {blocked ? <Badge variant="destructive">blocked</Badge> : null}
          {message.requires_ack ? (
            <Badge variant="outline">needs ack</Badge>
          ) : null}
        </div>
        {summary ? <p className="text-sm break-words">{summary}</p> : null}
        <Refs message={message} projectId={projectId} />
      </div>
    </article>
  );
}
