"use client";

/**
 * The Kanban board (Vol 4 §6.3, TITAN-FRT-024/025).
 *
 * Wires four things onto the pure core in `lib/kanban`: the REST snapshot, the
 * `task.kanban` frames, the user's drags, and the workflow that says which drags
 * are even moves. All reconciliation lives in `board-store.ts` — this component
 * decides only what to render and what to say out loud.
 *
 * Deviations from §6.3, each forced by the backend and detailed at its site:
 * columns are the global machine's statuses (`board-column.tsx`), there are no
 * WIP limits and no labels, the deep link carries a UUID (`task-card.tsx`), and
 * a drag is an ACTION rather than a status write (`workflow.ts`).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  type Announcements,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
  type ScreenReaderInstructions,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { toast } from "sonner";

import type { components } from "@titan/schemas";

import { AgentPicker } from "@/components/kanban/agent-picker";
import {
  BOARD_SCROLLER_CLASS,
  BoardColumn,
  BoardEmpty,
  BoardError,
  BoardSkeleton,
} from "@/components/kanban/board-column";
import { TaskCard } from "@/components/kanban/task-card";
import { TaskSheet } from "@/components/kanban/task-sheet";
import { TooltipProvider } from "@/components/ui/tooltip";
import { rolesCan } from "@/lib/auth/rbac";
import { useSession } from "@/lib/auth/session-context";
import {
  applyFrame,
  boardColumns,
  cardCount,
  effectiveStatus,
  hydrate,
  type BoardEffect,
} from "@/lib/kanban/board-store";
import { useBoardMoves } from "@/lib/kanban/use-moves";
import { unmetDependencyCounts } from "@/lib/kanban/deps";
import {
  BOARD_DRAG_INSTRUCTIONS,
  onDragCancelMessage,
  onDragEndMessage,
  onDragOverMessage,
  onDragStartMessage,
} from "@/lib/kanban/announcements";
import {
  useBoardState,
  useProjectGraph,
  useWorkflow,
} from "@/lib/kanban/hooks";
import { boardCoordinateGetter, nextCardId } from "@/lib/kanban/navigation";
import {
  moveIndex,
  permittedTargets,
  statusLabel,
} from "@/lib/kanban/workflow";
import { useAgents, useProjectBoard } from "@/lib/projects/hooks";
import { useShortcuts } from "@/lib/shortcuts/provider";
import type { Shortcut } from "@/lib/shortcuts/registry";
import { useConnection } from "@/lib/ws/connection-store";
import { useWsClient } from "@/lib/ws/manager";
import { useTopic } from "@/lib/ws/use-topic";

type AgentOut = components["schemas"]["AgentOut"];

/**
 * §10's Board rows, verbatim, for the `?` overlay (TITAN-FRT-048).
 *
 * Module scope so the array identity is stable across renders — `useShortcuts`
 * reads it at event time, but a stable reference keeps the registry from
 * churning on every board re-render.
 */
const BOARD_SHORTCUTS: Shortcut[] = (
  [
    ["board-navigate", "ArrowUp", "Move between cards"],
    ["board-open", "Enter", "Open the focused card"],
    ["board-lift", "Space", "Lift a card, then arrows to move"],
    ["board-drop", "Space", "Drop the lifted card"],
    ["board-cancel", "Escape", "Cancel the drag"],
  ] as const
).map(([id, keys, label]) => ({
  id,
  keys,
  label,
  group: "Board" as const,
  // Listed, never dispatched. Registering `ArrowUp` for real would make the
  // engine preventDefault it and kill the card navigation it documents.
  documentationOnly: true,
  run: () => {},
}));

/**
 * How long the acting agent's avatar pulses after an agent-driven move (§6.3).
 *
 * §6.3 wants the card itself to "animate (300 ms translate) to its new column".
 * It does not translate, and the honest reason is structural: a card moving
 * columns is unmounted from one list and mounted in another, so the DOM node is
 * destroyed and recreated — there is no element to translate. A real FLIP would
 * need the board to be one absolutely-positioned layout (or framer-motion's
 * `layoutId`, which cross-fades two elements rather than moving one).
 *
 * What actually happens: the card fades in at its destination over 300 ms
 * (`task-card.tsx`) and the acting agent's avatar pulses, so the move is still
 * noticed and still attributed. That is less than the spec asks for, and it is
 * what this board does.
 */
const AGENT_PULSE_MS = 1200;

export function BoardScreen({ projectId }: { projectId: string }) {
  const workflow = useWorkflow();
  const board = useProjectBoard(projectId);
  const graph = useProjectGraph(projectId);
  const agents = useAgents();
  // ONE ref for the board state, written only by `commit` (see useBoardState).
  const { stateRef, commit } = useBoardState(projectId);
  const state = stateRef.current;
  const client = useWsClient();
  const { status: connection } = useConnection();

  // The pure matcher, not `useCan`: which columns accept a card is decided per
  // (from, to) pair inside a memo, and a hook cannot be called in a loop. This
  // is the same function `useCan` wraps, so it resolves wildcards exactly as the
  // backend's `permission_granted` does.
  const session = useSession();
  const can = useCallback(
    (permission: string): boolean =>
      session ? rolesCan(session.roles, permission) : false,
    [session],
  );

  const [taskParam, setTaskParam] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [overStatus, setOverStatus] = useState<string | null>(null);
  const [animating, setAnimating] = useState<ReadonlyMap<string, string>>(
    new Map(),
  );
  const timers = useRef<number[]>([]);

  useTopic("projectTasks", projectId);

  // --- deep link: ?task=<uuid> ---------------------------------------------
  useEffect(() => {
    const read = (): void => {
      setTaskParam(new URLSearchParams(window.location.search).get("task"));
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  const openTask = useCallback((taskId: string | null): void => {
    const url = new URL(window.location.href);
    if (taskId === null) {
      url.searchParams.delete("task");
    } else {
      url.searchParams.set("task", taskId);
    }
    window.history.pushState({}, "", url);
    setTaskParam(taskId);
  }, []);

  // --- effects from the reducer --------------------------------------------
  const handleEffects = useCallback((effects: readonly BoardEffect[]): void => {
    for (const effect of effects) {
      if (effect.kind === "conflict") {
        // Server-wins: our optimistic move is already gone from the state; say
        // so rather than let the card silently jump under the user's hand.
        toast.warning(
          `“${effect.title}” moved to ${statusLabel(effect.actual)} — your move to ${statusLabel(effect.attempted)} was superseded.`,
        );
      } else {
        const id = effect.taskId;
        setAnimating((prev) => {
          const next = new Map(prev);
          next.set(id, effect.actorAgentId);
          return next;
        });
        const clear = window.setTimeout(() => {
          setAnimating((prev) => {
            if (!prev.has(id)) {
              return prev;
            }
            const next = new Map(prev);
            next.delete(id);
            return next;
          });
        }, AGENT_PULSE_MS);
        timers.current.push(clear);
      }
    }
  }, []);

  useEffect(
    () => () => {
      for (const timer of timers.current) {
        window.clearTimeout(timer);
      }
      timers.current = [];
    },
    [],
  );

  // --- REST snapshot --------------------------------------------------------
  const boardData = board.data;
  useEffect(() => {
    if (boardData) {
      commit(hydrate(stateRef.current, boardData));
    }
  }, [boardData, commit, stateRef]);

  // --- frames ---------------------------------------------------------------
  useEffect(() => {
    if (!client) {
      return;
    }
    return client.onAnyFrame((frame) => {
      const result = applyFrame(stateRef.current, frame);
      if (result.state === stateRef.current) {
        return; // not ours, or a replay — no state change, no effects
      }
      commit(result.state);
      handleEffects(result.effects);
    });
  }, [client, commit, handleEffects, stateRef]);

  const index = useMemo(
    () => (workflow.data ? moveIndex(workflow.data) : null),
    [workflow.data],
  );
  const agentsById = useMemo(() => {
    const map = new Map<string, AgentOut>();
    for (const agent of agents.data?.items ?? []) {
      map.set(agent.id, agent);
    }
    return map;
  }, [agents.data]);
  const projectAgents = useMemo(
    () => (agents.data?.items ?? []).filter((a) => a.project_id === projectId),
    [agents.data, projectId],
  );
  const unmet = useMemo(() => unmetDependencyCounts(graph.data), [graph.data]);

  const columns = useMemo(
    () => (workflow.data ? boardColumns(state, workflow.data.states) : []),
    [state, workflow.data],
  );
  // Navigation reads the grid from a ref: the keydown handler is stable, and a
  // handler that closed over `columns` would move focus by a stale layout after
  // an agent's frame re-grouped the board.
  const columnsRef = useRef(columns);
  columnsRef.current = columns;

  // --- the move -------------------------------------------------------------
  const {
    attemptMove,
    agentChoice,
    confirmAgent,
    cancelAgent,
    failureAnnouncement,
  } = useBoardMoves({ stateRef, commit, index });

  // --- dnd ------------------------------------------------------------------
  //
  // Mouse + Touch, NOT Pointer. §8 requires "long-press initiation on touch",
  // which is a different activation constraint from the mouse's 4 px, and one
  // sensor cannot hold two. Keeping PointerSensor alongside TouchSensor does not
  // work either — a touch fires pointer events too, so the pointer sensor would
  // claim the gesture after 4 px of finger travel, which both defeats the
  // long-press and makes the board's own horizontal scroller impossible to swipe
  // (every pan would lift a card instead). dnd-kit's documented answer to mixed
  // constraints is exactly this pair. The cost is pen input, which now arrives
  // through the browser's compatibility mouse events rather than natively.
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 4 } }),
    useSensor(TouchSensor, {
      // Hold, then drag. `tolerance` is the slop allowed DURING the delay: too
      // small and a resting thumb's micro-movement cancels the press before it
      // arms, too large and a deliberate scroll turns into a drag.
      activationConstraint: { delay: 300, tolerance: 8 },
    }),
    useSensor(KeyboardSensor, {
      // §10 gives Enter and Space different jobs on this screen: Enter OPENS a
      // card, Space lifts it. dnd-kit's default start/end codes are ['Space',
      // 'Enter'], which would make Enter lift instead — narrow it to Space.
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space"] },
      // One press, one column. dnd-kit's default nudges 25px, which is ~11
      // presses per column and ~150 across this board — see the getter.
      coordinateGetter: boardCoordinateGetter,
    }),
  );

  /*
   * TITAN-FRT-048 requires §10's Board bindings to APPEAR in the `?` overlay,
   * and that overlay is generated from the live registry — so a binding the
   * engine never sees is one no user can discover, however well it works.
   *
   * These are DOCUMENTATION rows with an inert `run`. The engine must not
   * dispatch them: it calls preventDefault() before running a match, so
   * claiming Space/Enter/arrows globally would break the very interaction they
   * describe (`isNativeActivation` in lib/shortcuts/keys.ts exists for exactly
   * that reason, and would ignore them anyway). The real handling stays where
   * it belongs — the dnd-kit KeyboardSensor above, and TaskCard's onKeyDown.
   */
  useShortcuts(BOARD_SHORTCUTS);

  // §10: arrows move focus between cards while nothing is lifted.
  const cardNodes = useRef(new Map<string, HTMLElement>());
  const registerCardRef = useCallback(
    (taskId: string, node: HTMLElement | null): void => {
      if (node) {
        cardNodes.current.set(taskId, node);
      } else {
        cardNodes.current.delete(taskId);
      }
    },
    [],
  );
  const navigate = useCallback((taskId: string, key: string): void => {
    const target = nextCardId(columnsRef.current, taskId, key);
    if (target) {
      cardNodes.current.get(target)?.focus();
    }
  }, []);

  const dragCard = dragging ? state.cards.get(dragging) : undefined;
  const dragFrom = dragging ? effectiveStatus(state, dragging) : null;

  const moveTargets = useMemo(
    () => permittedTargets(index, can, workflow.data?.states ?? []),
    [index, workflow.data, can],
  );

  const allowedTargets = useMemo(
    () => new Set(dragFrom ? (moveTargets.get(dragFrom) ?? []) : []),
    [moveTargets, dragFrom],
  );

  const screenReaderInstructions: ScreenReaderInstructions = useMemo(
    () => ({ draggable: BOARD_DRAG_INSTRUCTIONS }),
    [],
  );

  const announcements: Announcements = useMemo(
    () => ({
      onDragStart: ({ active }) => {
        const card = stateRef.current.cards.get(String(active.id));
        const from = effectiveStatus(stateRef.current, String(active.id));
        return onDragStartMessage({
          title: card?.title ?? "Task",
          fromStatus: from ?? "",
        });
      },
      onDragOver: ({ active, over }) => {
        const card = stateRef.current.cards.get(String(active.id));
        const from = effectiveStatus(stateRef.current, String(active.id)) ?? "";
        const to = over ? String(over.id) : null;
        return onDragOverMessage({
          title: card?.title ?? "Task",
          fromStatus: from,
          overStatus: to,
          allowed: to !== null && allowedTargets.has(to),
        });
      },
      onDragEnd: ({ active, over }) => {
        const card = stateRef.current.cards.get(String(active.id));
        const from = effectiveStatus(stateRef.current, String(active.id)) ?? "";
        const to = over ? String(over.id) : null;
        return onDragEndMessage({
          title: card?.title ?? "Task",
          fromStatus: from,
          overStatus: to,
          allowed: to !== null && allowedTargets.has(to),
        });
      },
      onDragCancel: ({ active }) => {
        const card = stateRef.current.cards.get(String(active.id));
        const from = effectiveStatus(stateRef.current, String(active.id)) ?? "";
        return onDragCancelMessage({
          title: card?.title ?? "Task",
          fromStatus: from,
        });
      },
    }),
    [allowedTargets, stateRef],
  );

  const onDragStart = (event: DragStartEvent): void => {
    setDragging(String(event.active.id));
  };
  const onDragOver = (event: DragOverEvent): void => {
    setOverStatus(event.over ? String(event.over.id) : null);
  };
  const onDragEnd = (event: DragEndEvent): void => {
    const taskId = String(event.active.id);
    setDragging(null);
    setOverStatus(null);
    if (!event.over) {
      return;
    }
    const to = String(event.over.id);
    if (!allowedTargets.has(to)) {
      return; // illegal or unpermitted — announced, and nothing is sent
    }
    attemptMove(taskId, to);
  };

  // --- render ---------------------------------------------------------------
  if (workflow.isLoading || board.isLoading) {
    return <BoardSkeleton />;
  }
  if (workflow.isError || board.isError) {
    return (
      <BoardError
        onRetry={() => {
          void board.refetch();
          void workflow.refetch();
        }}
      />
    );
  }
  // Ask the QUERY, not just the store. The store is populated by an effect, so
  // on the first committed render after the data resolves it is still empty —
  // gating on it alone flashes "No tasks yet" over every non-empty board.
  const queryIsEmpty = (boardData?.columns ?? []).every(
    (column) => column.tasks.length === 0,
  );
  if (queryIsEmpty && cardCount(state) === 0) {
    return <BoardEmpty />;
  }

  // The stream is per PROJECT, not per column: there is no such thing as one
  // column's feed failing. When the socket is down every column is equally
  // stale, so every column says so — §6.3's per-column badge, at the honest
  // granularity.
  const stale = connection !== "live";

  return (
    <TooltipProvider>
      <DndContext
        sensors={sensors}
        accessibility={{ announcements, screenReaderInstructions }}
        onDragStart={onDragStart}
        onDragOver={onDragOver}
        onDragEnd={onDragEnd}
        onDragCancel={() => {
          setDragging(null);
          setOverStatus(null);
        }}
      >
        {/* §8: the board scrolls in its OWN container (the shell gives the
            content region `min-w-0`), and snaps column-to-column under `lg` —
            see `BOARD_SCROLLER_CLASS` for why snapping stops there. */}
        <div className={BOARD_SCROLLER_CLASS}>
          {columns.map((column) => (
            <BoardColumn
              key={column.status}
              status={column.status}
              count={column.cards.length}
              isDropTarget={allowedTargets.has(column.status)}
              isRejecting={
                dragging !== null && !allowedTargets.has(column.status)
              }
              stale={stale}
            >
              {column.cards.map((card) => (
                <TaskCard
                  key={card.id}
                  card={card}
                  agent={
                    card.assignee_agent_id
                      ? agentsById.get(card.assignee_agent_id)
                      : undefined
                  }
                  unmetDeps={unmet.get(card.id) ?? 0}
                  draggable={can("task:transition")}
                  // Keyed by the COLUMN's status, not `card.status`. A column is
                  // a grouping by EFFECTIVE status (server truth with any live
                  // optimistic override laid over it), and `attemptMove`
                  // resolves the action from that same effective status — so
                  // this is the one key that cannot disagree with what the move
                  // will actually do while a drag of this card is in flight.
                  moveTargets={moveTargets.get(column.status) ?? []}
                  onMove={attemptMove}
                  pulsing={animating.get(card.id) === card.assignee_agent_id}
                  animating={animating.has(card.id)}
                  onOpen={openTask}
                  onNavigate={navigate}
                  registerRef={registerCardRef}
                />
              ))}
            </BoardColumn>
          ))}
        </div>

        <DragOverlay>
          {dragCard ? (
            <div className="w-60 rounded-md border bg-card p-2 text-sm shadow-lg">
              {dragCard.title}
              {overStatus && !allowedTargets.has(overStatus) ? (
                <p className="mt-1 text-[10px] text-muted-foreground">
                  {statusLabel(overStatus)} won’t accept this
                </p>
              ) : null}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>

      {/* A failed move is announced separately: dnd-kit's own live region has
          already said "moved to X" by the time the server refuses, so the
          rollback needs its own voice. */}
      {/* `role="alert"` rather than `role="status" aria-live="assertive"`.
          The pair worked — an explicit `aria-live` overrides the role's implicit
          `polite` — but it asks a reader to reconcile a role that means "polite"
          with an attribute that says "assertive", and the platform mapping for
          that contradiction is not something to rely on. `alert` IS
          assertive-by-role, which is what TITAN-FRT-043 asks for here: a move
          that failed and rolled back is the one board event a user must not miss
          while reading something else. */}
      <div role="alert" className="sr-only">
        {failureAnnouncement}
      </div>

      {/* Mounted only while a choice is pending, and KEYED by the task: the
          picker holds the selected agent in component state, and a component
          that merely returns null keeps its state — so a permanently-mounted
          picker would silently pre-arm the previous move's agent for the next
          one, and a confirm without re-picking would assign the wrong agent. */}
      {agentChoice ? (
        <AgentPicker
          key={`${agentChoice.taskId}:${agentChoice.action}`}
          request={agentChoice}
          agents={projectAgents}
          onConfirm={confirmAgent}
          onCancel={cancelAgent}
        />
      ) : null}

      <TaskSheet
        projectId={projectId}
        taskId={taskParam}
        graph={graph.data}
        agents={agentsById}
        onClose={() => openTask(null)}
      />
    </TooltipProvider>
  );
}
