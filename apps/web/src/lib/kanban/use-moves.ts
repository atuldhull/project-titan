"use client";

/**
 * Making a move: optimistic overlay → request → settle or roll back
 * (TITAN-FRT-015).
 *
 * Extracted from the board screen so the rollback path is testable at the real
 * code path rather than through dnd-kit's pointer plumbing, which needs layout
 * jsdom does not have. The screen supplies the drag; this owns what a drag
 * MEANS.
 */

import { useCallback, useState } from "react";
import { toast } from "sonner";

import {
  beginMove,
  effectiveStatus,
  failMove,
  settleMove,
  type BoardState,
} from "@/lib/kanban/board-store";
import { moveFailedMessage } from "@/lib/kanban/announcements";
import { useTransitionTask } from "@/lib/kanban/hooks";
import {
  resolveMove,
  statusLabel,
  type MoveIndex,
} from "@/lib/kanban/workflow";

/** A move paused on "which agent?" — see `agent-picker.tsx`. */
export interface PendingAgentChoice {
  readonly taskId: string;
  readonly title: string;
  readonly action: string;
  readonly toStatus: string;
  readonly assigneeAgentId: string | null;
}

export interface BoardMoves {
  /** Drop a card on a column. Resolves the action, or does nothing if illegal. */
  attemptMove: (taskId: string, toStatus: string) => void;
  /** Set when a move needs an agent named before it can be sent. */
  agentChoice: PendingAgentChoice | null;
  /** The user picked an agent — send the paused move. */
  confirmAgent: (agentId: string) => void;
  /** The user cancelled — the card never moved, so there is nothing to undo. */
  cancelAgent: () => void;
  /** The live announcement for a failed move, for the board's aria-live region. */
  failureAnnouncement: string | null;
}

export function useBoardMoves({
  stateRef,
  commit,
  index,
}: {
  /** Live board state. A ref, so a move never reads a stale snapshot. */
  stateRef: { current: BoardState };
  commit: (next: BoardState) => void;
  index: MoveIndex | null;
}): BoardMoves {
  const transition = useTransitionTask();
  const [agentChoice, setAgentChoice] = useState<PendingAgentChoice | null>(
    null,
  );
  const [failureAnnouncement, setFailureAnnouncement] = useState<string | null>(
    null,
  );

  const runMove = useCallback(
    (
      taskId: string,
      toStatus: string,
      action: string,
      actorAgentId?: string,
    ) => {
      const fire = (): void => {
        // Read the card and lay the override INSIDE fire, so a retry from the
        // toast is a whole attempt rather than a bare re-request. The failure
        // already rolled the override off; without re-laying it the card would
        // sit in its old column through the retry, and `settleMove` — which
        // takes the absence of an override to mean "a frame already spoke" —
        // would then discard the successful response.
        const card = stateRef.current.cards.get(taskId);
        const title = card?.title ?? "Task";
        const origin = effectiveStatus(stateRef.current, taskId) ?? "";

        // `commit` is the ref's only writer — see `useBoardState`.
        commit(beginMove(stateRef.current, taskId, toStatus));

        // `mutateAsync`, NOT `mutate` with callbacks. One `useMutation` backs
        // every card on the board, and its observer holds ONE callback slot:
        // `MutationObserver.mutate` overwrites `#mutateOptions` and detaches
        // itself from the previous mutation, so a second drag while a first is
        // in flight silently kills the first's onSuccess/onError — leaving a
        // failed move's override on the card forever, with no rollback and no
        // toast. The promise `mutateAsync` returns is per-call and settles
        // regardless, which is what makes concurrent drags safe.
        void transition
          .mutateAsync({ taskId, action, actorAgentId })
          .then((task) => {
            commit(settleMove(stateRef.current, taskId, task.status));
          })
          .catch((error: unknown) => {
            // Rollback is structural: dropping the override restores the card
            // to wherever the server still has it.
            commit(failMove(stateRef.current, taskId));
            setFailureAnnouncement(moveFailedMessage(title, origin));
            // The 409 is the interesting failure and the global mutation
            // handler ignores it (it only speaks 401/403). The server's
            // `detail` says WHY — "not all dependencies are done", "no
            // reflection on record", "the reviewer must differ from the
            // assignee" — none of which a client could have predicted. Surface
            // it verbatim instead of a generic "something went wrong".
            const detail =
              error instanceof Error && error.message
                ? error.message
                : "The move failed.";
            toast.error(
              `Couldn’t move “${title}” to ${statusLabel(toStatus)}. ${detail}`,
              {
                action: { label: "Retry", onClick: fire },
              },
            );
          });
      };
      fire();
    },
    [commit, stateRef, transition],
  );

  const attemptMove = useCallback(
    (taskId: string, toStatus: string): void => {
      if (!index) {
        return;
      }
      const from = effectiveStatus(stateRef.current, taskId);
      if (from === null) {
        return;
      }
      const move = resolveMove(index, from, toStatus);
      if (!move) {
        return; // no single action performs this — the drop was already refused
      }
      if (move.requires_actor_agent) {
        // Pause rather than send: the API 409s without an agent, and the flag
        // saying so is the same one it raises that 409 from.
        const card = stateRef.current.cards.get(taskId);
        setAgentChoice({
          taskId,
          title: card?.title ?? "Task",
          action: move.action,
          toStatus,
          assigneeAgentId: card?.assignee_agent_id ?? null,
        });
        return;
      }
      runMove(taskId, toStatus, move.action);
    },
    [index, runMove, stateRef],
  );

  const confirmAgent = useCallback(
    (agentId: string): void => {
      if (agentChoice) {
        runMove(
          agentChoice.taskId,
          agentChoice.toStatus,
          agentChoice.action,
          agentId,
        );
      }
      setAgentChoice(null);
    },
    [agentChoice, runMove],
  );

  return {
    attemptMove,
    agentChoice,
    confirmAgent,
    cancelAgent: () => setAgentChoice(null),
    failureAnnouncement,
  };
}
