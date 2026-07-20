/**
 * The 13 surfaces of Vol 4 §6, as the sweep walks them.
 *
 * Thirteen, not fourteen: §6.2 is one screen specification covering the projects
 * LIST and a project's DETAIL, so they share an entry and the detail route rides
 * along in `alsoVisit`. TITAN-FRT-041 is written against this count.
 */
export interface Screen {
  /** §6.x label, used in test titles. */
  readonly name: string;
  readonly path: (projectId: string) => string;
  /** Extra routes belonging to the same screen spec. */
  readonly alsoVisit?: readonly ((projectId: string) => string)[];
}

export const SCREENS: readonly Screen[] = [
  { name: "6.1 Organization Overview", path: () => "/org" },
  {
    name: "6.2 Projects",
    path: () => "/projects",
    alsoVisit: [(pid) => `/projects/${pid}`, () => "/projects/new"],
  },
  { name: "6.3 Kanban Board", path: (pid) => `/projects/${pid}/board` },
  { name: "6.4 Task Graph", path: (pid) => `/projects/${pid}/graph` },
  { name: "6.5 Agent Grid", path: () => "/agents" },
  { name: "6.6 Conversations", path: () => "/conversations" },
  { name: "6.7 Logs Viewer", path: () => "/logs" },
  {
    name: "6.8 Architecture Viewer",
    path: (pid) => `/projects/${pid}/architecture`,
  },
  {
    name: "6.9 Deployments",
    path: () => "/deployments",
    alsoVisit: [(pid) => `/projects/${pid}/deployments`],
  },
  { name: "6.10 Monitoring", path: () => "/monitoring" },
  { name: "6.11 Memory Explorer", path: () => "/memory" },
  { name: "6.12 Plugin Manager", path: () => "/plugins" },
  {
    name: "6.13 Settings",
    path: () => "/settings/profile",
    alsoVisit: [
      () => "/settings/providers",
      () => "/settings/members",
      () => "/settings/api-keys",
      () => "/settings/notifications",
      () => "/settings/budgets",
      () => "/settings/appearance",
    ],
  },
];

/** Every route the sweep visits, screen paths and their siblings alike. */
export function allRoutes(projectId: string): { name: string; url: string }[] {
  return SCREENS.flatMap((screen) => [
    { name: screen.name, url: screen.path(projectId) },
    ...(screen.alsoVisit ?? []).map((build) => ({
      name: `${screen.name} → ${build(projectId)}`,
      url: build(projectId),
    })),
  ]);
}
