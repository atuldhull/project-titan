/**
 * An agent's initials.
 *
 * Lives in `lib/` because two features need it — the board's cards and the
 * graph's nodes — and TITAN-FRT-049 forbids `components/graph` importing from
 * `components/kanban`. Sharing through `lib/` is exactly what that rule asks for.
 *
 * Why initials at all: `AgentOut` is `{id, project_id, role, name, status,
 * model_binding, config, current_task_id, …}`. There is no avatar, image, icon or
 * colour on it, and the roster's `AgentDefinition` adds a display name but no art
 * either. This is an identicon, not a photo.
 */

/** "backend-bot" -> "BB", "qa" -> "QA". */
export function initials(name: string): string {
  const parts = name.split(/[\s\-_.]+/u).filter(Boolean);
  if (parts.length === 0) {
    return "??";
  }
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
