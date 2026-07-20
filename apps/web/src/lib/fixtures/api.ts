import type { components, paths } from "@titan/schemas";

import type { ArchitecturePackage } from "@/lib/graph/architecture";

/**
 * Deterministic API fixture corpus, shared by the Playwright E2E sweep and the
 * Storybook harness.
 *
 * EVERY fixture is typed through {@link GetOk}, which resolves the real 200
 * response type out of the generated OpenAPI `paths` — so `npm run typecheck`
 * fails if a fixture drifts from the contract the app actually consumes. That
 * is deliberate and load-bearing: two earlier phases shipped green suites whose
 * hand-rolled fakes were wrong in the same way the code under test was wrong,
 * and a fake that cannot compile against the contract is the cheapest guard
 * against repeating it. One corpus behind both harnesses means a story and a
 * spec cannot disagree about what the API returns.
 *
 * This module is pure data and types: it must stay free of any Playwright
 * import so browser code can pull it in. The E2E route wiring that serves these
 * bodies lives in `e2e/support/api.ts`.
 */

type Schemas = components["schemas"];

type JsonBody<T> = T extends { content: { "application/json": infer B } }
  ? B
  : never;

/** The 200 body of `GET P`, straight out of the generated contract. */
type GetOk<P extends keyof paths> = paths[P] extends {
  get: { responses: { 200: infer R } };
}
  ? JsonBody<R>
  : never;

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID_2 = "11111111-1111-4111-8111-111111111112";
const AGENT_ID = "22222222-2222-4222-8222-222222222222";
const TASK_ID = "33333333-3333-4333-8333-333333333333";
const USER_ID = "00000000-0000-4000-8000-0000000000e2";
const NOW = "2026-07-01T12:00:00Z";

export const FIXTURE_IDS = {
  project: PROJECT_ID,
  agent: AGENT_ID,
  task: TASK_ID,
  user: USER_ID,
  /** The display name the palette must find by fuzzy search. */
  projectName: "netflix-clone",
} as const;

// --- entities ---------------------------------------------------------------

export const project = (
  over: Partial<Schemas["ProjectOut"]> = {},
): Schemas["ProjectOut"] => ({
  id: PROJECT_ID,
  name: "netflix-clone",
  display_name: "Netflix Clone",
  brief: "A streaming catalogue with recommendations and a watch list.",
  status: "active",
  mode: "supervised",
  llm_profile: "balanced",
  version: 3,
  created_at: NOW,
  created_by: USER_ID,
  ...over,
});

export const PROJECTS: Schemas["ProjectOut"][] = [
  project(),
  project({
    id: PROJECT_ID_2,
    name: "spotify-clone",
    display_name: "Spotify Clone",
    brief: "Playlists, playback and a recommendation feed.",
    status: "paused",
  }),
];

export const task = (
  over: Partial<Schemas["TaskOut"]> = {},
): Schemas["TaskOut"] => ({
  id: TASK_ID,
  project_id: PROJECT_ID,
  title: "Add the watch list",
  spec: "Users can save a title and see it on the home row.",
  status: "in_progress",
  priority: 2,
  version: 1,
  created_at: NOW,
  assignee_agent_id: AGENT_ID,
  completed_at: null,
  created_by_agent_id: null,
  estimate_points: 3,
  milestone_id: null,
  parent_id: null,
  reviewer_agent_id: null,
  sprint_id: null,
  wfl_lane: "build",
  ...over,
});

export const TASKS: Schemas["TaskOut"][] = [
  task(),
  task({
    id: "33333333-3333-4333-8333-333333333334",
    title: "Wire the recommendations endpoint",
    status: "todo",
  }),
  task({
    id: "33333333-3333-4333-8333-333333333335",
    title: "Playback telemetry",
    status: "done",
    completed_at: NOW,
  }),
];

export const AGENTS: Schemas["AgentOut"][] = [
  {
    id: AGENT_ID,
    name: "Backend Engineer",
    role: "backend",
    roster_id: "eng_backend",
    status: "idle",
    project_id: PROJECT_ID,
    current_task_id: null,
    config: {},
    model_binding: {},
    version: 1,
    created_at: NOW,
  },
  {
    id: "22222222-2222-4222-8222-222222222223",
    name: "QA Engineer",
    role: "qa",
    roster_id: "qa_engineer",
    status: "paused",
    project_id: PROJECT_ID,
    current_task_id: null,
    config: {},
    model_binding: {},
    version: 1,
    created_at: NOW,
  },
];

export const AGENT_DEFINITIONS: GetOk<"/api/v1/agent-definitions"> = [
  {
    agent_id: "eng_backend",
    display_name: "Backend Engineer",
    tier: 2,
    supervisor: "eng_lead",
    kpis: ["tests pass", "no boundary violations"],
    tool_grants: ["repo.read", "repo.write"],
    system_prompt_template: "You are the backend engineer.",
    authority: {
      decides_alone: ["module layout"],
      must_escalate: [],
      veto: false,
    },
    failure_policy: {
      escalates_to_human: false,
      mode: "default",
      skip_retries_on: [],
      watchdog_restart: true,
      notes: null,
    },
    memory_scopes: { read: ["project", "code_patterns"], write: ["project"] },
    model_policy: {
      max_output_tokens: 4096,
      temperature: 0.2,
      tier: "standard",
      structured_output_schema: null,
    },
  },
];

export const BOARD: GetOk<"/api/v1/projects/{project_id}/board"> = {
  project_id: PROJECT_ID,
  columns: [
    {
      status: "todo",
      tasks: [
        {
          id: TASKS[1].id,
          title: TASKS[1].title,
          status: "todo",
          priority: 2,
          assignee_agent_id: null,
          estimate_points: 2,
          sprint_id: null,
        },
      ],
    },
    {
      status: "in_progress",
      tasks: [
        {
          id: TASK_ID,
          title: TASKS[0].title,
          status: "in_progress",
          priority: 1,
          assignee_agent_id: AGENT_ID,
          estimate_points: 3,
          sprint_id: null,
        },
      ],
    },
    { status: "review", tasks: [] },
    {
      status: "done",
      tasks: [
        {
          id: TASKS[2].id,
          title: TASKS[2].title,
          status: "done",
          priority: 3,
          assignee_agent_id: AGENT_ID,
          estimate_points: 1,
          sprint_id: null,
        },
      ],
    },
  ],
};

export const WORKFLOW: GetOk<"/api/v1/workflow"> = {
  states: ["todo", "in_progress", "review", "done"],
  transitions: [
    {
      action: "start",
      from_status: "todo",
      to_status: "in_progress",
      required_permission: "task:write",
      requires_actor_agent: false,
    },
    {
      action: "submit",
      from_status: "in_progress",
      to_status: "review",
      required_permission: "task:write",
      requires_actor_agent: false,
    },
    {
      action: "approve",
      from_status: "review",
      to_status: "done",
      required_permission: "task:write",
      requires_actor_agent: false,
    },
  ],
};

export const GRAPH: GetOk<"/api/v1/projects/{project_id}/graph"> = {
  project_id: PROJECT_ID,
  nodes: TASKS.map((item) => ({
    id: item.id,
    title: item.title,
    status: item.status,
  })),
  edges: [{ task_id: TASKS[0].id, depends_on_id: TASKS[1].id }],
};

export const DEPLOYMENTS: GetOk<"/api/v1/projects/{project_id}/deployments"> = [
  {
    id: "44444444-4444-4444-8444-444444444444",
    project_id: PROJECT_ID,
    name: "netflix-clone-web",
    status: "running",
    health: "healthy",
    detail: "3 services up",
    compose_file: "docker-compose.yml",
    services: ["web", "api", "db"],
    port: 8080,
    url: "http://localhost:8080",
    task_id: null,
    created_at: NOW,
    updated_at: NOW,
  },
];

export const ARTIFACT_VERSIONS: GetOk<"/api/v1/projects/{project_id}/handoffs/{stage}/artifact-versions"> =
  {
    project_id: PROJECT_ID,
    stage: "architect_design",
    versions: [
      {
        id: "55555555-5555-4555-8555-555555555555",
        project_id: PROJECT_ID,
        stage: "architect_design",
        artifact_type: "architecture",
        version: 2,
        schema_version: 1,
        submitted_by: "architect",
        created_at: NOW,
      },
    ],
  };

export const LOGS: GetOk<"/api/v1/logs"> = {
  items: [
    {
      id: "log-1",
      ts_ns: "1782000000000000000",
      line: "run finished",
      level: "info",
      event: "run.finished",
      module: "titan.runtime.turn",
      labels: { service: "api" },
      fields: { duration_ms: 812 },
      project_id: PROJECT_ID,
      agent_id: AGENT_ID,
      task_id: TASK_ID,
      request_id: "req-1",
      trace_id: "trace-1",
      span_id: "span-1",
    },
    {
      id: "log-2",
      ts_ns: "1782000000000000001",
      line: "gateway call failed after 3 attempts",
      level: "error",
      event: "gateway.error",
      module: "titan.llm.gateway",
      labels: { service: "api" },
      fields: {},
      project_id: PROJECT_ID,
      agent_id: null,
      task_id: null,
      request_id: "req-2",
      trace_id: "trace-2",
      span_id: "span-2",
    },
  ],
  next_cursor: null,
};

export const USAGE: Schemas["UsageSummary"] = {
  start: "2026-06-24T00:00:00Z",
  end: "2026-07-01T00:00:00Z",
  group_by: "provider",
  truncated: false,
  total: {
    calls: 412,
    cost_usd: "12.480000",
    input_tokens: 918_004,
    output_tokens: 121_887,
    cached_tokens: 40_112,
  },
  groups: [
    {
      key: "anthropic",
      calls: 250,
      cost_usd: "9.120000",
      input_tokens: 600_000,
      output_tokens: 80_000,
      cached_tokens: 30_000,
    },
    {
      key: "ollama",
      calls: 162,
      cost_usd: "3.360000",
      input_tokens: 318_004,
      output_tokens: 41_887,
      cached_tokens: 10_112,
    },
  ],
  series: [
    {
      bucket_at: "2026-06-30T00:00:00Z",
      key: "anthropic",
      calls: 120,
      cost_usd: "4.100000",
      input_tokens: 300_000,
      output_tokens: 40_000,
    },
    {
      bucket_at: "2026-06-30T00:00:00Z",
      key: "ollama",
      calls: 80,
      cost_usd: "1.600000",
      input_tokens: 150_000,
      output_tokens: 20_000,
    },
  ],
};

/**
 * The same window with nothing in it — see {@link FixtureRule.emptyBody}.
 *
 * `total` is a roll-up of `groups`, so it has to be zeroed by hand: emptying the
 * collections alone leaves the KPI row claiming 412 calls over zero rows.
 * `cost_usd` stays a string because the contract types it as one (the backend
 * serialises `Decimal`, never a float), and "0.000000" keeps its six places so
 * the empty story exercises the same formatter as the populated one.
 */
export const USAGE_EMPTY: Schemas["UsageSummary"] = {
  start: USAGE.start,
  end: USAGE.end,
  group_by: USAGE.group_by,
  truncated: false,
  total: {
    calls: 0,
    cost_usd: "0.000000",
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
  },
  groups: [],
  series: [],
};

export const MEMORY_RECORDS: GetOk<"/api/v1/memory/records"> = {
  items: [
    {
      id: "66666666-6666-4666-8666-666666666666",
      title: "Prefer SQLAlchemy Core over the ORM in this repo",
      kind: "lesson",
      record_subtype: "lessons",
      scope: "project",
      status: "active",
      project_id: PROJECT_ID,
      source_agent: "eng_backend",
      source_task_id: TASK_ID,
      superseded_by: null,
      importance: 0.82,
      decay_score: 0.91,
      access_count: 14,
      created_at: NOW,
    },
  ],
  next_cursor: null,
};

export const MEMORY_FACETS: GetOk<"/api/v1/memory/facets"> = {
  kinds: [{ value: "lesson", count: 1 }],
  scopes: [{ value: "project", count: 1 }],
  statuses: [{ value: "active", count: 1 }],
  agents: [{ value: "eng_backend", count: 1 }],
  subtypes: [{ value: "lessons", count: 1 }],
};

export const PLUGINS: GetOk<"/api/v1/plugins"> = [
  {
    plugin_id: "titan.sample",
    name: "Sample Connector",
    description: "A first-party sample plugin.",
    plugin_version: "1.0.0",
    origin: "first_party",
    content_hash: "abc123",
    manifest: { name: "Sample Connector" },
  },
];

export const INSTALLS: GetOk<"/api/v1/plugins/installs"> = [
  {
    plugin_id: "titan.sample",
    state: "enabled",
    installed_version: "1.0.0",
    content_hash: "abc123",
    error_count: 0,
    last_error: "",
    tools: ["sample.echo"],
    manifest: { name: "Sample Connector" },
    granted_permissions: {},
  },
];

export const PROFILE: GetOk<"/api/v1/auth/profile"> = {
  user_id: USER_ID,
  email: "operator@titan.local",
  display_name: "Local Operator",
  auth_provider: "local",
  is_active: true,
  roles: ["owner"],
};

export const NOTIFICATIONS: GetOk<"/api/v1/notifications"> = {
  items: [
    {
      id: "77777777-7777-4777-8777-777777777777",
      user_id: USER_ID,
      title: "Deployment succeeded",
      body: "netflix-clone-web is healthy.",
      category: "deployment",
      severity: "info",
      link: "/deployments",
      read_at: null,
      created_at: NOW,
      version: 1,
    },
  ],
  next_cursor: null,
};

export const LLM_SETTINGS: GetOk<"/api/v1/settings/llm"> = {
  default_alias: "sonnet",
  overridden: false,
  agent_profile: null,
  embeddings_default_alias: "embed-small",
  aliases: [],
  providers: [],
  by_agent: {},
  fallbacks: {},
};

export const MEMBERS: GetOk<"/api/v1/members"> = [
  {
    user_id: USER_ID,
    email: "operator@titan.local",
    display_name: "Local Operator",
    is_active: true,
    roles: ["owner"],
    project_grants: [],
  },
];

export const SESSIONS: GetOk<"/api/v1/auth/sessions"> = [
  {
    family_id: "88888888-8888-4888-8888-888888888888",
    created_at: NOW,
    last_used_at: NOW,
    expires_at: "2026-07-08T12:00:00Z",
    current: true,
    revoked: false,
    expired: false,
  },
];

export const API_KEYS: GetOk<"/api/v1/auth/api-keys"> = [
  {
    id: "99999999-9999-4999-8999-999999999999",
    name: "CI runner",
    prefix: "titan_ci",
    agent_id: null,
    created_at: NOW,
    last_used_at: NOW,
    expires_at: null,
    revoked_at: null,
  },
];

export const PREFERENCES: GetOk<"/api/v1/notification-preferences"> = {
  available_channels: ["in_app", "email"],
  unavailable_channels: ["plugins"],
  categories: [
    {
      category: "deployment",
      channels: ["in_app"],
      severity: "info",
      is_default: true,
    },
  ],
};

export const RUNS: GetOk<"/api/v1/projects/{project_id}/runs"> = {
  items: [
    {
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      task_id: TASK_ID,
      agent_id: AGENT_ID,
      status: "succeeded",
      attempt: 1,
      model: "claude-sonnet-5",
      provider: "anthropic",
      cost_usd: "0.041200",
      tokens_in: 8104,
      tokens_out: 912,
      input: {},
      output_summary: "Added the watch list endpoint and its tests.",
      error: null,
      started_at: NOW,
      finished_at: NOW,
      created_at: NOW,
      version: 1,
    },
  ],
  next_cursor: null,
};

export const MESSAGES = {
  items: [
    {
      id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      project_id: PROJECT_ID,
      thread_id: "thread-1",
      kind: "status_update",
      priority: "normal",
      schema_version: "1",
      from_agent: "eng_backend",
      to_agent: "eng_lead",
      sender_agent_id: null,
      sender_user_id: null,
      task_id: TASK_ID,
      correlation_id: null,
      requires_ack: false,
      body: { text: "Watch list endpoint is up; tests green." },
      created_at: NOW,
      version: 1,
    },
  ] satisfies Schemas["MessageOut"][],
  next_cursor: null,
};

export const TASK_PAGE: GetOk<"/api/v1/projects/{project_id}/tasks"> = {
  items: TASKS,
  next_cursor: null,
};

export const APPROVALS: GetOk<"/api/v1/projects/{project_id}/tool-approvals"> =
  {
    items: [
      {
        id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        project_id: PROJECT_ID,
        task_id: TASK_ID,
        agent_id: AGENT_ID,
        tool: "repo.write",
        args_hash: "d1e2f3",
        args_redacted: { path: "src/api/watchlist.py" },
        status: "pending",
        resolved_at: null,
        resolved_by: null,
        created_at: NOW,
        version: 1,
      },
    ],
    next_cursor: null,
  };

export const MILESTONES: GetOk<"/api/v1/projects/{project_id}/milestones"> = {
  items: [
    {
      id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      project_id: PROJECT_ID,
      title: "MVP catalogue",
      status: "in_progress",
      due_at: "2026-08-01T00:00:00Z",
      created_at: NOW,
      version: 1,
    },
  ],
  next_cursor: null,
};

export const SPRINTS: GetOk<"/api/v1/projects/{project_id}/sprints"> = {
  items: [
    {
      id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
      project_id: PROJECT_ID,
      number: 3,
      goal: "Watch list end to end",
      status: "active",
      starts_at: NOW,
      ends_at: "2026-07-15T00:00:00Z",
      created_at: NOW,
      version: 1,
    },
  ],
  next_cursor: null,
};

export const DECISIONS: GetOk<"/api/v1/projects/{project_id}/decisions"> = {
  items: [
    {
      id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
      project_id: PROJECT_ID,
      title: "Postgres over DynamoDB",
      context: "The catalogue is relational and the team knows SQL.",
      decision: "Use PostgreSQL 16.",
      alternatives: "DynamoDB, MongoDB",
      consequences: "One more container in Compose; joins stay cheap.",
      status: "accepted",
      authority: "architect",
      decided_by_agent_id: null,
      supersedes_id: null,
      created_at: NOW,
      version: 1,
    },
  ],
  next_cursor: null,
};

export const DERIVED_ARTIFACT: GetOk<"/api/v1/projects/{project_id}/artifacts/{kind}"> =
  {
    kind: "roadmap",
    artifact_version: 2,
    content_hash: "aa11bb22",
    content_json: {},
    content_md:
      "## Roadmap\n\n| Milestone | Status |\n|---|---|\n| MVP catalogue | In progress |\n",
    created_at: NOW,
  };

/**
 * The H5 `ArchitecturePackage` §6.8 renders — a REAL one.
 *
 * This was `{ layers: [], components: [] }`, which is not merely thin: `layers`
 * is not a field of the package at all, and zero components is the one input for
 * which the Architecture Viewer renders its EMPTY copy ("This version has no
 * components."). So §6.8's "Populated" story was a populated story of an empty
 * screen — the version picker, the layer filter, the diff control and the whole
 * `GraphShell` dynamic boundary had no coverage in any state but loading, and
 * the visual baseline locked that in as correct.
 *
 * `satisfies ArchitecturePackage` is the guard against it drifting back. The
 * OpenAPI type of `body` is `{[key: string]: unknown}` — the backend stores the
 * package as opaque JSONB (see `lib/graph/architecture.ts`), so the contract
 * cannot check this one and the fixture has to check itself against the same
 * hand-written mirror `parsePackage` narrows to.
 *
 * Six components across all four `CleanLayer` values, with dependencies that
 * cross layers: enough for the canvas to draw real nodes AND real edges, and
 * enough that the layer filter has something to remove for every option it
 * offers.
 */
const ARCHITECTURE_PACKAGE = {
  components: [
    { name: "Title", layer: "entities", depends_on: [] },
    { name: "WatchList", layer: "entities", depends_on: ["Title"] },
    {
      name: "AddToWatchList",
      layer: "use_cases",
      depends_on: ["WatchList", "Title"],
    },
    { name: "RecommendTitles", layer: "use_cases", depends_on: ["Title"] },
    {
      name: "WatchListRouter",
      layer: "interface_adapters",
      depends_on: ["AddToWatchList"],
    },
    {
      name: "PostgresWatchListRepo",
      layer: "frameworks",
      depends_on: ["WatchList"],
    },
  ],
  adrs: [
    {
      id: "ADR-001",
      title: "PostgreSQL for the catalogue",
      decision: "Use PostgreSQL 16 for titles, watch lists and playback state.",
      rationale: "The catalogue is relational and joins stay cheap.",
    },
    {
      id: "ADR-002",
      title: "Repositories own all SQL",
      decision: "Use cases depend on repository interfaces, never on the ORM.",
      rationale: "Keeps the entity and use-case layers free of framework code.",
    },
  ],
} satisfies ArchitecturePackage;

export const HANDOFF_ARTIFACT: GetOk<"/api/v1/projects/{project_id}/handoffs/{stage}/artifact-versions/{version}"> =
  {
    id: "55555555-5555-4555-8555-555555555555",
    project_id: PROJECT_ID,
    stage: "architect_design",
    artifact_type: "architecture",
    version: 2,
    schema_version: 1,
    submitted_by: "architect",
    body: ARCHITECTURE_PACKAGE,
    created_at: NOW,
  };

// --- routing ----------------------------------------------------------------

type Json = Record<string, unknown> | unknown[];

export interface FixtureRule {
  readonly method?: string;
  /** Matched against the API path (the `/api/titan` prefix already stripped). */
  readonly pattern: RegExp;
  readonly body: Json;
  readonly status?: number;
  /**
   * The body a story's "empty" state should serve, when emptying `body`
   * mechanically produces a lie.
   *
   * `emptied()` in the story harness clears arrays and leaves scalars alone,
   * which is the right default for ~30 of these fixtures and cannot ever be
   * right for a body that carries a ROLL-UP of a collection it also contains.
   * The array heuristic sees `total.calls: 412` as an unrelated scalar; it has
   * no way to know that field means "the sum of `groups`", so §6.10 Monitoring's
   * empty story rendered a full KPI row ($12.48 / 918.0k / 412) stacked above
   * "No data in range" — a state the API can never return, snapshotted as a
   * baseline. No heuristic over shape can express that relationship, so the
   * fixture states it directly.
   */
  readonly emptyBody?: Json;
}

export const API_FIXTURES: FixtureRule[] = [
  {
    pattern: /^\/api\/v1\/projects$/,
    body: { items: PROJECTS, next_cursor: null },
  },
  { pattern: /^\/api\/v1\/projects\/[^/:]+$/, body: project() },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/board$/, body: BOARD },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/graph$/, body: GRAPH },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/tasks$/, body: TASK_PAGE },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/runs$/, body: RUNS },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/messages$/, body: MESSAGES },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/deployments$/, body: DEPLOYMENTS },
  {
    pattern: /^\/api\/v1\/projects\/[^/]+\/handoffs\/[^/]+\/artifact-versions$/,
    body: ARTIFACT_VERSIONS,
  },
  {
    // The versioned read must come after the list route above, or its pattern
    // would also swallow `/artifact-versions` with no version.
    pattern:
      /^\/api\/v1\/projects\/[^/]+\/handoffs\/[^/]+\/artifact-versions\/\d+$/,
    body: HANDOFF_ARTIFACT,
  },
  {
    pattern: /^\/api\/v1\/projects\/[^/]+\/tool-approvals$/,
    body: APPROVALS,
  },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/milestones$/, body: MILESTONES },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/sprints$/, body: SPRINTS },
  { pattern: /^\/api\/v1\/projects\/[^/]+\/decisions$/, body: DECISIONS },
  {
    pattern: /^\/api\/v1\/projects\/[^/]+\/artifacts\/[^/]+$/,
    body: DERIVED_ARTIFACT,
  },
  { pattern: /^\/api\/v1\/workflow$/, body: WORKFLOW },
  {
    pattern: /^\/api\/v1\/agents$/,
    body: { items: AGENTS, next_cursor: null },
  },
  { pattern: /^\/api\/v1\/agent-definitions$/, body: AGENT_DEFINITIONS },
  { pattern: /^\/api\/v1\/logs$/, body: LOGS },
  { pattern: /^\/api\/v1\/usage$/, body: USAGE, emptyBody: USAGE_EMPTY },
  // Phase 66: this carried `status: 500` alongside a fully populated body and
  // no explanation — review residue from Phase 65, where an agent probed the
  // error path and the override was never taken back out. The cost was silent:
  // every sweep that "covered" /memory was measuring its error panel, so the
  // results list's populated layout had never been through axe or the 375 px
  // walk at all. A fixture that contradicts its own body deserves more
  // suspicion than a failing assertion — the suite stayed green either way.
  { pattern: /^\/api\/v1\/memory\/records$/, body: MEMORY_RECORDS },
  { pattern: /^\/api\/v1\/memory\/facets$/, body: MEMORY_FACETS },
  { pattern: /^\/api\/v1\/plugins$/, body: PLUGINS },
  { pattern: /^\/api\/v1\/plugins\/installs$/, body: INSTALLS },
  { pattern: /^\/api\/v1\/notifications$/, body: NOTIFICATIONS },
  { pattern: /^\/api\/v1\/notification-preferences$/, body: PREFERENCES },
  { pattern: /^\/api\/v1\/settings\/llm$/, body: LLM_SETTINGS },
  { pattern: /^\/api\/v1\/members$/, body: MEMBERS },
  {
    pattern: /^\/api\/v1\/roles$/,
    body: ["owner", "admin", "maintainer", "operator", "viewer"],
  },
  { pattern: /^\/api\/v1\/auth\/profile$/, body: PROFILE },
  { pattern: /^\/api\/v1\/auth\/sessions$/, body: SESSIONS },
  { pattern: /^\/api\/v1\/auth\/api-keys$/, body: API_KEYS },
];
