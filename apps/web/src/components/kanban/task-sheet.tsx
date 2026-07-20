"use client";

/**
 * The task detail Sheet — right slide-over, deep-linked via `?task=` (§6.3).
 *
 * §6.3 asks for: description, acceptance criteria, dependency links, linked
 * PR/conversation, and full event history. What is actually behind each:
 *
 * - **Description** — real (`TaskOut.spec`, markdown).
 * - **Acceptance criteria** — NOT a field. The intake agent flattens a story's
 *   criteria into `spec` as a markdown bullet list (`stories.spec_text()`), so
 *   they are prose inside the description and nothing more. Structured Gherkin
 *   criteria do exist in the H2 backlog ARTIFACT, but its story ids are
 *   plan-local strings and `tasks` has no `story_id`, so the join is impossible
 *   without a migration. Rendering `spec` is therefore the honest whole of it.
 * - **Dependency links** — real, derived from the project graph.
 * - **Linked PR** — DOES NOT EXIST. No pull_requests table, no pr/branch/sha
 *   column on tasks or runs, no endpoint. The only PR data anywhere is inside a
 *   handoff artifact keyed by plan-local ids. Not rendered; saying so beats an
 *   empty "Pull request" row that implies one is coming.
 * - **Conversation + event history** — real, and the same thing: every
 *   transition writes a message against the task, so the thread IS the history.
 *
 * The deep link is `?task=<uuid>`, not §6.3's `?task=T-231` — see `shortTaskId`.
 */

import ReactMarkdown from "react-markdown";

import type { components } from "@titan/schemas";

import { AgentAvatar } from "@/components/kanban/agent-avatar";
import { shortTaskId } from "@/components/kanban/task-card";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useTask, useTaskMessages, useTaskRuns } from "@/lib/kanban/hooks";
import { taskDependencies } from "@/lib/kanban/deps";
import type { Graph } from "@/lib/kanban/deps";
import { statusLabel } from "@/lib/kanban/workflow";

type AgentOut = components["schemas"]["AgentOut"];
type MessageOut = components["schemas"]["MessageOut"];

/**
 * One history row's text. A transition message's body is
 * `{action, from, to, comment?}` — `kind` is LOSSY (start/block/merge/fail all
 * collapse onto `progress_report`), so what happened is `body.action`. A message
 * with no action is ordinary conversation, shown as its kind.
 */
export function historyLine(message: MessageOut): string {
  const body = message.body as Record<string, unknown> | null;
  const action = typeof body?.action === "string" ? body.action : null;
  if (!action) {
    return message.kind.replace(/_/g, " ");
  }
  const from = typeof body?.from === "string" ? statusLabel(body.from) : "?";
  const to = typeof body?.to === "string" ? statusLabel(body.to) : "?";
  return `${action.replace(/_/g, " ")}: ${from} → ${to}`;
}

export function TaskSheet({
  projectId,
  taskId,
  graph,
  agents,
  onClose,
}: {
  projectId: string;
  taskId: string | null;
  graph: Graph | undefined;
  agents: ReadonlyMap<string, AgentOut>;
  onClose: () => void;
}) {
  const task = useTask(projectId, taskId);
  const messages = useTaskMessages(projectId, taskId);
  const runs = useTaskRuns(projectId, taskId);
  const deps = taskDependencies(graph, taskId ?? "");

  return (
    <Sheet open={taskId !== null} onOpenChange={(open) => !open && onClose()}>
      {/*
       * §8: "detail panels → bottom Sheets" from 1024 down. A right slide-over
       * at 375 px is a full-width panel that slides in sideways and covers the
       * board completely, with its close button in the far corner — the pattern
       * exists because a bottom sheet keeps the reading position and puts the
       * dismiss gesture under the thumb.
       *
       * Done in CSS on the content rather than by switching `side`, because
       * `side` is a JS prop and this app has no `matchMedia` (and adding one
       * unmocked would break the vitest suite). So the sheet stays `side="right"`
       * — the default — and the `max-lg:` utilities re-anchor it: release the
       * top so `inset-y-0`'s bottom:0 is the only edge that binds, cap the
       * height, round the leading corners, move the border to the top edge, and
       * swap the slide direction so it does not enter sideways onto a bottom
       * edge. Those all beat their unvariant counterparts because Tailwind emits
       * media-query variants after plain utilities.
       *
       * **The width is done the other way round, and must be.** The obvious
       * `max-lg:max-w-none` does NOT work: Tailwind v4 emits `max-*` variants
       * BEFORE `min-*` ones, so `SheetContent`'s own `sm:max-w-sm` (and any
       * `sm:` cap we passed) lands later in the stylesheet and wins wherever
       * both match — which is 640–1023, i.e. most of the band this whole change
       * exists to fix. Verified against the compiled stylesheet, not assumed.
       * So the cap is expressed as min-width steps instead: cleared at `sm` (to
       * displace the component's own, via tailwind-merge) and re-applied at `lg`,
       * where the sheet really is a right-hand slide-over again.
       *
       * The breakpoint is `lg` (1024), matching §8's table: bottom sheet at
       * 768–1023 and below, right slide-over at 1024 and up.
       *
       * One caveat, found while checking the above and reported rather than
       * worked around: the `slide-*`/`animate-*` utilities `ui/sheet.tsx` uses
       * (and the pair added here to match them) come from `tailwindcss-animate`,
       * which this app does not install and `globals.css` does not import — so
       * Tailwind emits no rule for any of them and the sheet does not currently
       * animate in from ANY direction. The classes are kept in step with the
       * component's so that adding the plugin makes both correct at once,
       * instead of making the bottom sheet slide in from the right.
       */}
      <SheetContent className="w-full overflow-y-auto sm:max-w-none lg:max-w-lg max-lg:inset-x-0 max-lg:top-auto max-lg:h-auto max-lg:max-h-[85vh] max-lg:rounded-t-xl max-lg:border-t max-lg:border-l-0 max-lg:data-[state=closed]:slide-out-to-bottom max-lg:data-[state=open]:slide-in-from-bottom">
        {task.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : task.isError || !task.data ? (
          <div className="p-4">
            <SheetHeader className="p-0">
              <SheetTitle>Couldn’t load this task</SheetTitle>
              <SheetDescription>
                {task.error instanceof Error
                  ? task.error.message
                  : "The task could not be fetched."}
              </SheetDescription>
            </SheetHeader>
            {/* §8: ≥44 px touch targets under `md`. This was bare underlined
                text — roughly 16 px tall — and it is the only control on a
                sheet that has failed to load, so it is the last thing that
                should need a precise tap. `inline-flex` because `min-h` has no
                effect on an inline box. */}
            <button
              type="button"
              onClick={() => void task.refetch()}
              className="mt-3 inline-flex items-center text-sm underline max-md:min-h-11"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="space-y-4 p-4">
            <SheetHeader className="p-0">
              <SheetTitle className="text-base">{task.data.title}</SheetTitle>
              <SheetDescription className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-xs">
                  {shortTaskId(task.data.id)}
                </span>
                <Badge variant="secondary">
                  {statusLabel(task.data.status)}
                </Badge>
                {task.data.estimate_points !== null ? (
                  <Badge variant="outline">
                    {task.data.estimate_points} pts
                  </Badge>
                ) : null}
                <span className="inline-flex items-center gap-1 text-xs">
                  Assignee
                  <AgentAvatar
                    agent={
                      task.data.assignee_agent_id
                        ? agents.get(task.data.assignee_agent_id)
                        : undefined
                    }
                  />
                </span>
              </SheetDescription>
            </SheetHeader>

            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground">
                Description
              </h4>
              {task.data.spec ? (
                <div className="prose prose-sm mt-1 max-w-none dark:prose-invert">
                  {/* No rehype-raw: task specs are agent-authored, so raw HTML
                      must never be interpreted. */}
                  <ReactMarkdown>{task.data.spec}</ReactMarkdown>
                </div>
              ) : (
                <p className="mt-1 text-sm text-muted-foreground">
                  No description.
                </p>
              )}
              {/* §6.3 lists acceptance criteria as their own section. They are not
                  a field — intake flattens them into the markdown above — so
                  there is nothing separate to render. */}
            </section>

            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground">
                Dependencies
              </h4>
              {deps.blockedBy.length === 0 && deps.blocking.length === 0 ? (
                <p className="mt-1 text-sm text-muted-foreground">
                  No dependencies.
                </p>
              ) : (
                <div className="mt-1 space-y-1">
                  {deps.blockedBy.map((dep) => (
                    <div
                      key={dep.node.id}
                      className="flex items-center gap-2 text-sm"
                    >
                      <Badge
                        variant={dep.satisfied ? "secondary" : "outline"}
                        className={
                          dep.satisfied ? "" : "border-warning text-warning"
                        }
                      >
                        {dep.satisfied ? "done" : statusLabel(dep.node.status)}
                      </Badge>
                      <span className="truncate">Needs: {dep.node.title}</span>
                    </div>
                  ))}
                  {deps.blocking.map((node) => (
                    <div
                      key={node.id}
                      className="flex items-center gap-2 text-sm"
                    >
                      <Badge variant="secondary">
                        {statusLabel(node.status)}
                      </Badge>
                      <span className="truncate text-muted-foreground">
                        Blocks: {node.title}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground">
                History
              </h4>
              {messages.isLoading ? (
                <Skeleton className="mt-1 h-12 w-full" />
              ) : messages.isError ? (
                <p role="alert" className="mt-1 text-sm text-destructive">
                  Couldn’t load this task’s history.
                </p>
              ) : (messages.data?.items ?? []).length === 0 ? (
                <p className="mt-1 text-sm text-muted-foreground">
                  Nothing recorded yet. Note that system moves (the scheduler
                  readying a task) leave no entry.
                </p>
              ) : (
                <ol className="mt-1 space-y-1">
                  {(messages.data?.items ?? []).map((message) => (
                    <li
                      key={message.id}
                      className="flex items-baseline gap-2 text-sm"
                    >
                      <time
                        dateTime={message.created_at}
                        className="shrink-0 font-mono text-[10px] text-muted-foreground"
                      >
                        {new Date(message.created_at).toLocaleString()}
                      </time>
                      <span>{historyLine(message)}</span>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground">
                Runs
              </h4>
              {(runs.data?.items ?? []).length === 0 ? (
                <p className="mt-1 text-sm text-muted-foreground">
                  No run attempts.
                </p>
              ) : (
                <ol className="mt-1 space-y-1">
                  {(runs.data?.items ?? []).map((run) => (
                    <li
                      key={run.id}
                      className="flex items-center gap-2 text-sm"
                    >
                      <Badge variant="secondary">#{run.attempt}</Badge>
                      <span>{run.status}</span>
                      {run.model ? (
                        <span className="truncate font-mono text-[10px] text-muted-foreground">
                          {run.model}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ol>
              )}
              {/* §6.3 also lists a linked PR here. There is no pull-request data
                  in the backend at all — no table, no column, no endpoint — so
                  the section is absent rather than permanently empty. */}
            </section>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
