"use client";

import { useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";

import { unwrap } from "@titan/api-client";
import type { components } from "@titan/schemas";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import { useCan } from "@/lib/auth/use-can";
import { useResolveApproval } from "@/lib/projects/hooks";

type ToolApprovalOut = components["schemas"]["ToolApprovalOut"];

interface PendingApproval {
  readonly projectId: string;
  readonly approval: ToolApprovalOut;
}

/**
 * The "Needs Your Attention" queue (§6.1) for human tool approvals.
 *
 * Approvals are per-project (`GET /projects/{id}/tool-approvals`) — there is no
 * org-wide approval endpoint — so the Overview fans out across the projects it
 * already lists. New approvals arrive within 2 s because the gate now emits an
 * approval/critical notification, and `notification.created` invalidates the
 * approvals keys (see lib/ws/reconcile.ts); no polling involved.
 *
 * Two clicks to act (TITAN-FRT-022): the item button opens the dialog, and
 * Approve/Deny resolves it.
 */
export function AttentionApprovals({
  projectIds,
}: {
  projectIds: readonly string[];
}) {
  const [selected, setSelected] = useState<PendingApproval | null>(null);

  const results = useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: [...queryKeys.projectApprovals(pid), "pending"],
      queryFn: async () =>
        unwrap(
          await browserApi().GET(
            "/api/v1/projects/{project_id}/tool-approvals",
            {
              params: {
                path: { project_id: pid },
                query: { status: "pending", limit: 50 },
              },
            },
          ),
        ),
    })),
  });

  const isLoading = results.some((r) => r.isLoading);
  const isError = results.some((r) => r.isError);
  const pending: PendingApproval[] = results.flatMap((result, index) =>
    (result.data?.items ?? []).map((approval) => ({
      projectId: projectIds[index],
      approval,
    })),
  );

  if (isLoading) {
    return (
      <div className="rounded-lg border bg-card p-3">
        <Skeleton className="h-4 w-48" />
        <Skeleton className="mt-2 h-8 w-64" />
      </div>
    );
  }

  // Never render "nothing needs you" on a failure — that would be a false
  // all-clear over pending approvals (§6 conventions: inline failed scope).
  if (isError && pending.length === 0) {
    return (
      <div className="rounded-lg border border-dashed bg-card p-3 text-sm text-destructive">
        Couldn’t load pending approvals.
      </div>
    );
  }

  if (pending.length === 0) {
    return null;
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3">
        <ShieldAlert aria-hidden className="size-4 text-warning" />
        <span className="text-sm font-semibold">
          Needs Your Attention ({pending.length})
        </span>
        <div className="flex flex-wrap gap-2">
          {pending.map((item) => (
            <Button
              key={item.approval.id}
              size="sm"
              variant="outline"
              onClick={() => setSelected(item)}
            >
              Approve {item.approval.tool}
            </Button>
          ))}
        </div>
      </div>

      <ApprovalDialog item={selected} onClose={() => setSelected(null)} />
    </>
  );
}

/** The approval dialog: context + Approve/Deny (see the Comment note below). */
export function ApprovalDialog({
  item,
  onClose,
}: {
  item: PendingApproval | null;
  onClose: () => void;
}) {
  const canResolve = useCan("tool_approval:write");
  const resolve = useResolveApproval(item?.projectId ?? "");

  if (!item) {
    return null;
  }
  const { approval } = item;

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Approve “{approval.tool}”?</DialogTitle>
          <DialogDescription>
            Agent <span className="font-mono">{approval.agent_id}</span> is
            waiting on a human decision before this tool runs.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Tool</dt>
          <dd className="font-mono">{approval.tool}</dd>
          <dt className="text-muted-foreground">Agent</dt>
          <dd className="font-mono">{approval.agent_id}</dd>
          {approval.task_id ? (
            <>
              <dt className="text-muted-foreground">Task</dt>
              <dd className="truncate font-mono text-xs">{approval.task_id}</dd>
            </>
          ) : null}
          <dt className="text-muted-foreground">Requested</dt>
          <dd>{new Date(approval.created_at).toLocaleString()}</dd>
        </dl>

        <div>
          <p className="text-xs font-medium text-muted-foreground">
            Arguments (redacted)
          </p>
          <pre className="mt-1 max-h-40 overflow-auto rounded-md border bg-muted p-2 font-mono text-xs">
            {JSON.stringify(approval.args_redacted, null, 2)}
          </pre>
        </div>

        {/* §6.1 asks for Approve/Reject/Comment. The API's resolution body is
            empty with extra="forbid", so a comment has nowhere to go — shipping
            a comment box would be a lie. Stated plainly instead. */}
        <p className="text-xs text-muted-foreground">
          Comments aren’t supported by the approval API yet — only approve or
          deny are recorded.
        </p>

        {!canResolve ? (
          <p className="text-xs text-destructive">
            You don’t have permission to resolve approvals.
          </p>
        ) : null}
        {resolve.isError ? (
          <p role="alert" className="text-xs text-destructive">
            {resolve.error instanceof Error
              ? resolve.error.message
              : "Couldn’t record that decision."}{" "}
            The agent is still waiting.
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="outline"
            disabled={!canResolve || resolve.isPending}
            onClick={() =>
              resolve.mutate(
                { approvalId: approval.id, action: "deny" },
                { onSuccess: onClose },
              )
            }
          >
            Deny
          </Button>
          <Button
            disabled={!canResolve || resolve.isPending}
            onClick={() =>
              resolve.mutate(
                { approvalId: approval.id, action: "approve" },
                { onSuccess: onClose },
              )
            }
          >
            {resolve.isPending ? "Working…" : "Approve"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
