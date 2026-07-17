"use client";

/**
 * The ERROR context drawer: ±50 lines around a line, plus its links (§6.7).
 *
 * The window comes from the BUFFER, not a fetch. That is the honest scope: the
 * buffer is contiguous around any line it holds, so ±50 lines from it are the
 * real neighbours. Re-fetching them would return the same lines through a
 * slower path, and the ring is capped at 50k precisely so this is cheap.
 */
import Link from "next/link";
import { Copy } from "lucide-react";
import { toast } from "sonner";

import type { LogLine } from "@titan/api-client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { copyText, formatClock } from "@/lib/logs/export";
import { levelStyle } from "@/components/logs/log-row";
import { contextWindow } from "@/lib/logs/context";
import type { Ring } from "@/lib/logs/ring";
import { cn } from "@/lib/utils";

export function LogContextDrawer({
  line,
  ring,
  onClose,
}: {
  readonly line: LogLine | null;
  readonly ring: Ring;
  readonly onClose: () => void;
}) {
  const window = line ? contextWindow(ring, line.id) : [];

  return (
    <Sheet open={line !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 sm:max-w-3xl"
      >
        <SheetHeader>
          <SheetTitle className="text-sm">
            {line
              ? `${formatClock(line.ts_ns)} · ${levelStyle(line.level).label}`
              : "Context"}
          </SheetTitle>
        </SheetHeader>

        {line ? (
          <>
            <div className="flex flex-wrap items-center gap-2 border-b p-3">
              {/* §6.7 wants links to the owning agent/task. Only these two are
                  real: a task_id is a uuid the board can open, and an agent_id
                  is the roster id the agent drawer is keyed by. Both are
                  present only when the emitter bound them — request-scoped and
                  turn-scoped work does; a module-level log line does not. */}
              {line.task_id && line.project_id ? (
                <Button asChild size="sm" variant="outline">
                  <Link
                    href={`/projects/${line.project_id}/board?task=${line.task_id}`}
                  >
                    Open task
                  </Link>
                </Button>
              ) : null}
              {line.agent_id ? (
                <Button asChild size="sm" variant="outline">
                  <Link href={`/agents/${line.agent_id}`}>Open agent</Link>
                </Button>
              ) : null}
              {line.request_id ? (
                <Badge variant="secondary" className="font-mono text-[10px]">
                  request_id={line.request_id}
                </Badge>
              ) : null}
              {line.trace_id ? (
                <Badge variant="secondary" className="font-mono text-[10px]">
                  trace_id={line.trace_id}
                </Badge>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto gap-1"
                onClick={() => {
                  void navigator.clipboard
                    .writeText(copyText(line))
                    .then(() => toast.success("Line copied"))
                    // clipboard needs a secure context and permission; a silent
                    // no-op would look like the button is broken.
                    .catch(() =>
                      toast.error("Couldn’t copy — clipboard unavailable"),
                    );
                }}
              >
                <Copy className="h-3 w-3" aria-hidden />
                Copy line
              </Button>
            </div>

            {!line.task_id && !line.agent_id ? (
              <p className="border-b px-3 py-2 text-[11px] text-muted-foreground">
                This line carries no task or agent id, so there is nothing to
                link to. Only request-scoped and agent-turn work binds them.
              </p>
            ) : null}

            <div className="flex-1 overflow-auto p-2 font-mono text-[11px] leading-5">
              {window.map((neighbour) => (
                <div
                  key={neighbour.id}
                  className={cn(
                    "flex gap-2 px-1",
                    neighbour.id === line.id &&
                      "rounded bg-muted font-semibold",
                  )}
                >
                  <span className="shrink-0 text-muted-foreground tabular-nums">
                    {formatClock(neighbour.ts_ns)}
                  </span>
                  <span
                    className={cn(
                      "w-12 shrink-0",
                      levelStyle(neighbour.level).className,
                    )}
                  >
                    {levelStyle(neighbour.level).label}
                  </span>
                  <span className="whitespace-pre-wrap break-all">
                    {neighbour.line}
                  </span>
                </div>
              ))}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
