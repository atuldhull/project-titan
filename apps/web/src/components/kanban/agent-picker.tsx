"use client";

/**
 * Names the agent for a move that requires one (§6.3 drag → transition).
 *
 * `assign` and the review actions are meaningless without an agent — `assign`
 * names the assignee, `approve`/`request_changes` name the reviewer — and the
 * API 409s without one. Which actions those are is not decided here: it is
 * `requires_actor_agent` on the published workflow, the same flag the server
 * raises its 409 from.
 *
 * So a drag onto those columns pauses here instead of firing a request that is
 * guaranteed to fail. Cancelling rolls the card back exactly like a failure.
 */

import { useState } from "react";

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
import { actionLabel, statusLabel } from "@/lib/kanban/workflow";

type AgentOut = components["schemas"]["AgentOut"];

export interface AgentPickerRequest {
  readonly taskId: string;
  readonly title: string;
  readonly action: string;
  readonly toStatus: string;
  /** The assignee, when known — a reviewer may not be the same agent. */
  readonly assigneeAgentId: string | null;
}

/**
 * Mount this only while a choice is pending, KEYED by the request — `selected`
 * is component state, and returning null does not reset it. The board does both;
 * `request` is non-nullable here so that contract cannot be forgotten.
 */
export function AgentPicker({
  request,
  agents,
  onConfirm,
  onCancel,
}: {
  request: AgentPickerRequest;
  agents: readonly AgentOut[];
  onConfirm: (agentId: string) => void;
  onCancel: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);

  const isReview =
    request.action === "approve" || request.action === "request_changes";
  // TITAN-BCK-016: a task can never reach done on a self-review. The server
  // enforces it; offering the assignee here would just manufacture a 409.
  const eligible = isReview
    ? agents.filter((a) => a.id !== request.assigneeAgentId)
    : agents;

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {actionLabel(request.action)} “{request.title}”
          </DialogTitle>
          <DialogDescription>
            Moving this to {statusLabel(request.toStatus)} needs{" "}
            {isReview ? "a reviewer" : "an agent to assign"}.
          </DialogDescription>
        </DialogHeader>

        {eligible.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {isReview
              ? "No agent other than the assignee is available to review this."
              : "This project has no agents to assign."}
          </p>
        ) : (
          <ul className="max-h-64 space-y-1 overflow-y-auto">
            {eligible.map((agent) => (
              <li key={agent.id}>
                <button
                  type="button"
                  onClick={() => setSelected(agent.id)}
                  aria-pressed={selected === agent.id}
                  className={
                    selected === agent.id
                      ? "w-full rounded-md border border-primary bg-accent p-2 text-left text-sm"
                      : "w-full rounded-md border p-2 text-left text-sm hover:bg-muted"
                  }
                >
                  <span className="font-medium">{agent.name}</span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    {agent.role} · {agent.status}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            disabled={selected === null}
            onClick={() => selected !== null && onConfirm(selected)}
          >
            {actionLabel(request.action)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
