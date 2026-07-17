/**
 * Structured React Query key factory (Vol 4 §4.1, TITAN-FRT-017).
 *
 * Every server-state key is built here so the WebSocket reconciliation layer and
 * the mutation layer invalidate the exact same tuples the hooks read — the §4.1
 * convention `['project', pid, 'tasks', filters]`. Keys are `readonly` tuples so
 * a typo is a type error, not a silent cache miss.
 */

export type TaskFilters = Readonly<Record<string, string | number | boolean>>;

export const queryKeys = {
  // NOTE: there is deliberately no single root prefix. Keys are rooted at their
  // domain (`project`, `notifications`, …) so the WS layer can invalidate one
  // slice precisely; to clear everything use `queryClient.clear()`. (An earlier
  // `all: ["titan"]` prefix matched NOTHING and would have silently no-opped.)

  me: () => ["me"] as const,

  /** The task state machine (GET /api/v1/workflow) — static app metadata, not
   *  project state, so it is rooted on its own and never invalidated. */
  workflow: () => ["workflow"] as const,

  projects: () => ["projects"] as const,
  project: (pid: string) => ["project", pid] as const,
  projectTasks: (pid: string, filters: TaskFilters = {}) =>
    ["project", pid, "tasks", filters] as const,
  /** Filter-agnostic prefix — invalidates every filtered task list at once. */
  projectTasksAll: (pid: string) => ["project", pid, "tasks"] as const,
  projectRuns: (pid: string) => ["project", pid, "runs"] as const,
  /** One agent's runs in one project — the ONLY answer to "what is this agent
   *  doing" (`agents.current_task_id` has no writer anywhere and
   *  `agents.status` only ever holds idle|paused, so a run is where the live
   *  task, the model actually in use, the token counts and the error text are).
   *  Nested under projectRuns so a run frame's invalidation reaches it. */
  projectAgentRuns: (pid: string, agentId: string) =>
    ["project", pid, "runs", "agent", agentId] as const,
  projectDeployments: (pid: string) => ["project", pid, "deployments"] as const,
  projectMessages: (pid: string) => ["project", pid, "messages"] as const,
  /** One agent's own output — nested under projectMessages so a message frame
   *  refreshes the agent drawer that is open over it. */
  agentMessages: (pid: string, agentId: string) =>
    ["project", pid, "messages", "agent", agentId] as const,

  // The three task-scoped keys below are deliberately nested UNDER their
  // project-scoped prefix rather than rooted at "task". That is what makes the
  // task Sheet live: `queryKeysForFrame` already invalidates projectTasksAll /
  // projectMessages / projectRuns, and each of these is a prefix-match away — so
  // a task frame refreshes the open Sheet with no new entry in the frame map.
  /** One task in full (the Sheet needs `spec`, which BoardCard lacks). */
  task: (pid: string, taskId: string) =>
    ["project", pid, "tasks", "detail", taskId] as const,
  /** A task's thread — its conversation AND its transition history. */
  taskMessages: (pid: string, taskId: string) =>
    ["project", pid, "messages", "task", taskId] as const,
  /** A task's run attempts. */
  taskRuns: (pid: string, taskId: string) =>
    ["project", pid, "runs", "task", taskId] as const,
  /** The Phase-41 board: columns of tasks by workflow state — the only real
   *  progress/blocked signal the backend exposes. */
  projectBoard: (pid: string) => ["project", pid, "board"] as const,
  /** The dependency DAG — the only readable source of task dependencies
   *  (`/tasks/{id}/dependencies` is POST-only). */
  projectGraph: (pid: string) => ["project", pid, "graph"] as const,
  projectSprints: (pid: string) => ["project", pid, "sprints"] as const,
  projectMilestones: (pid: string) => ["project", pid, "milestones"] as const,
  projectArtifact: (pid: string, kind: string) =>
    ["project", pid, "artifact", kind] as const,
  projectClarifications: (pid: string) =>
    ["project", pid, "clarifications"] as const,
  /** The ADR records behind the architecture panel (Vol 4 §6.8). */
  projectDecisions: (pid: string) => ["project", pid, "decisions"] as const,
  /** A stage's revision history — for the architecture version timeline. */
  artifactVersions: (pid: string, stage: string) =>
    ["project", pid, "artifact-versions", stage] as const,
  /** One immutable revision. A version is never overwritten, so it never goes
   *  stale — keyed by version so each is cached forever and diffs are free. */
  artifactVersion: (pid: string, stage: string, version: number) =>
    ["project", pid, "artifact-versions", stage, version] as const,
  projectApprovals: (pid: string) => ["approvals", pid] as const,
  /** Prefix over every project's approvals — the Overview queue fans out per
   *  project, and an incoming approval notification invalidates them all. */
  approvalsAll: () => ["approvals"] as const,

  /** The RUNTIME agent registry (GET /api/v1/agents) — a Page<AgentOut>. */
  agents: () => ["agents"] as const,
  agent: (agentId: string) => ["agents", agentId] as const,
  /** The DECLARATIVE roster (GET /api/v1/agent-definitions) — an
   *  AgentDefinition[], and a different endpoint, shape and lifetime from
   *  `agents()`.
   *
   *  These two shared the `["agents"]` key until Phase 61, which is a real bug
   *  and not a tidiness point: one cache entry held either a Page (an object)
   *  or a definition list (an array) depending on which screen mounted first,
   *  and with staleTime 30s the loser did not refetch. /org -> /agents inside
   *  the window handed the roster a Page and it threw on `.filter`. */
  agentDefinitions: () => ["agent-definitions"] as const,

  notifications: (filters: { unread?: boolean } = {}) =>
    ["notifications", filters] as const,
  /** The bell badge count — kept separate so it can refetch independently. */
  notificationsUnread: () => ["notifications", "unread"] as const,

  orgActivity: () => ["org", "activity"] as const,
  monitoringSummary: () => ["monitoring", "summary"] as const,

  /** The Loki-backed log history (GET /api/v1/logs), keyed by its filters.
   *
   *  Rooted on its own rather than under `project`: logs are org-wide
   *  infrastructure with no tenancy — `?project=` is a filter over the JSON
   *  body, not an authorization scope — so nesting them under a project key
   *  would let a task frame invalidate a window of log history that has nothing
   *  to do with it. Nothing invalidates this key: history is immutable, and the
   *  tail advances by fetching a NEW window rather than refetching an old one. */
  /**  Only the OPENING page is cached: the tail, the back-scan and the gap
   *  fills each resume from a cursor the previous call produced, so every step
   *  would be a new key and a new cache entry that nothing ever reads again.
   *  Those are plain awaited calls in the screen, not queries. */
  logs: (filters: Readonly<Record<string, unknown>> = {}) =>
    ["logs", filters] as const,

  /** The LLM cost ledger (GET /api/v1/usage). Keyed by the range NAME, never
   *  by the resolved `start`: the timestamp moves every millisecond, so keying
   *  on it would mint a cache entry per render. */
  usage: (filters: Readonly<Record<string, unknown>> = {}) =>
    ["usage", filters] as const,

  /** The Memory Explorer's browse list, keyed by its facets. */
  memoryRecords: (filters: Readonly<Record<string, unknown>> = {}) =>
    ["memory", "records", filters] as const,
  /** One record + its chunks + its full ledger trail (FRT-034's provenance). */
  memoryRecord: (recordId: string) => ["memory", "records", recordId] as const,
  /** The counts behind the tier tree. */
  memoryFacets: (filters: Readonly<Record<string, unknown>> = {}) =>
    ["memory", "facets", filters] as const,
  /** A semantic search. Cached per query so re-selecting a result does not
   *  re-embed and re-search. */
  memorySearch: (query: string, scope: string) =>
    ["memory", "search", query, scope] as const,
} as const;

export type QueryKeys = typeof queryKeys;
