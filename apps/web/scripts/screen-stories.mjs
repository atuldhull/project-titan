// The 13 screens of Vol 4 §6 and the four stories each must have
// (TITAN-FRT-002). Shared by `check-story-coverage.mjs` and the visual
// regression suite, and the reason both agree on what "a screen" is.
//
// Thirteen, not fourteen: §6.2 is ONE screen specification covering the
// projects list and a project's detail. That is the same count
// `e2e/support/screens.ts` walks and that TITAN-FRT-041 is written against —
// two pinning tests there fail if it ever changes, so the two lists cannot
// drift apart silently.
//
// Plain `.mjs` rather than TypeScript because the coverage checker is a Node
// script that runs before any build step; the stories import nothing from here,
// they just have to match the titles.

/** Story names required for every screen, in state order. */
export const REQUIRED_STATES = ["Loading", "Empty", "Error", "Populated"];

/**
 * `title` is the Storybook title the screen's stories must declare. The
 * `Screens/` namespace is deliberate: it separates a screen's four-state
 * stories from the pre-existing component stories (Overview/KpiCard,
 * Agents/AgentCard, …), which cover pieces rather than screens and are not what
 * TITAN-FRT-002 asks for.
 */
export const SCREEN_STORIES = [
  { section: "6.1", title: "Screens/6.1 Organization Overview" },
  { section: "6.2", title: "Screens/6.2 Projects" },
  { section: "6.3", title: "Screens/6.3 Kanban Board" },
  { section: "6.4", title: "Screens/6.4 Task Graph" },
  { section: "6.5", title: "Screens/6.5 Agent Status Grid" },
  { section: "6.6", title: "Screens/6.6 Live Agent Conversations" },
  { section: "6.7", title: "Screens/6.7 Logs Viewer" },
  { section: "6.8", title: "Screens/6.8 Architecture Viewer" },
  { section: "6.9", title: "Screens/6.9 Deployment Dashboard" },
  { section: "6.10", title: "Screens/6.10 Monitoring" },
  { section: "6.11", title: "Screens/6.11 Memory Explorer" },
  { section: "6.12", title: "Screens/6.12 Plugin Manager" },
  { section: "6.13", title: "Screens/6.13 Settings" },
];
