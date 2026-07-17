/**
 * TITAN-FRT-034: every memory entry SHALL display full provenance (creating
 * agent, source entity, timestamp) and its usage history.
 *
 * All four parts are real, and the backend already returns them together —
 * `GET /memory/records/{id}` is record + chunks + the complete append-only
 * ledger trail. This module is only the reading of it, and the honest scoring
 * of which links actually resolve.
 *
 * The one gap, stated precisely because the first version got it wrong:
 * **`source_agent` is a Text NAME, and `/agents/[id]` resolves by the
 * agents-table UUID.** The agent detail screen does `agents.find(a => a.id ===
 * agentId)` against a uuid, while `source_agent` holds `agent.memory_agent`, a
 * roster id, or a user id — none of which is that uuid. So any agent link built
 * from `source_agent` is dead by construction. It is rendered as TEXT, never a
 * link, which is the same conclusion Phase 61 reached for `from_agent`:
 * attribution is by name, not resolvable to one agent's page. The name is real
 * provenance; it is simply not clickable.
 */
import type { MemoryRecordDetail } from "@titan/api-client";

export interface ProvenanceLink {
  readonly kind: "agent" | "task" | "project" | "record";
  readonly label: string;
  /** null when the reference is real but nothing can resolve it. */
  readonly href: string | null;
  readonly reason?: string;
}

export function provenanceOf(detail: MemoryRecordDetail): ProvenanceLink[] {
  const record = detail.record;
  const links: ProvenanceLink[] = [];

  if (record.source_agent) {
    links.push({
      kind: "agent",
      label: record.source_agent,
      href: null,
      reason:
        "source_agent is a name; the agent page is addressed by id, so it cannot be linked",
    });
  }

  // The one typed FK on the record besides the project: a task really is a row
  // the board can open.
  if (record.source_task_id && record.project_id) {
    links.push({
      kind: "task",
      label: `Task ${record.source_task_id.slice(0, 8)}`,
      href: `/projects/${record.project_id}/board?task=${record.source_task_id}`,
    });
  } else if (record.source_task_id) {
    links.push({
      kind: "task",
      label: `Task ${record.source_task_id.slice(0, 8)}`,
      href: null,
      // The board route is project-scoped; an org-scoped record naming a task
      // gives no project to open it in.
      reason:
        "the record has no project, so there is no board to open the task on",
    });
  }

  if (record.project_id) {
    links.push({
      kind: "project",
      label: `Project ${record.project_id.slice(0, 8)}`,
      href: `/projects/${record.project_id}`,
    });
  }

  if (record.superseded_by) {
    links.push({
      kind: "record",
      label: "Superseded by a newer revision",
      href: `/memory?record=${record.superseded_by}`,
    });
  }

  return links;
}

export interface UsageEvent {
  readonly at: string;
  readonly action: string;
  readonly actor: string;
  readonly taskId: string | null;
}

/**
 * The usage history: who read this record, and when.
 *
 * The ledger is append-only (a DB trigger rejects UPDATE and DELETE), so this
 * is the complete trail and not a sample. `read` is the action that answers
 * §6.11's "which retrievals used it"; the others are the record's own life
 * (`write`, `promote`, `supersede`, `prune`).
 */
export function usageHistory(detail: MemoryRecordDetail): UsageEvent[] {
  return (detail.ledger ?? []).map((entry) => ({
    at: entry.at,
    action: entry.action,
    actor: entry.actor_agent,
    taskId: entry.task_id ?? null,
  }));
}

export function readCount(detail: MemoryRecordDetail): number {
  return usageHistory(detail).filter((e) => e.action === "read").length;
}

/**
 * Is the provenance chain complete, in FRT-034's sense?
 *
 * "Complete" means all three of the parts the requirement names are present —
 * a creating agent, a source entity, and a timestamp. A record with no source
 * task still has provenance (an agent wrote it at a time); what it lacks is a
 * source ENTITY, and the screen says so rather than showing a blank.
 */
export function provenanceComplete(detail: MemoryRecordDetail): boolean {
  const record = detail.record;
  return Boolean(
    record.source_agent && record.created_at && record.source_task_id,
  );
}
