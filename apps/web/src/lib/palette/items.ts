import type { AgentOut, ProjectOut, TaskOut } from "@titan/api-client";

import { orgNavItems, projectNavItems } from "@/components/shell/nav-items";
import type { Searchable } from "./search";

/**
 * The command palette's candidate set (TITAN-FRT-007: "any screen, project,
 * agent, or task").
 *
 * Screen items are generated PER PROJECT, not just for the project currently in
 * the URL. That is what makes the specified `kan net` → netflix-clone board work
 * from anywhere: the two terms match two different words of one item's haystack
 * ("Board … netflix-clone"), which cannot happen if project-scoped screens only
 * exist for the active project.
 */

export type PaletteGroup = "Screens" | "Projects" | "Agents" | "Tasks";

export interface PaletteItem extends Searchable {
  readonly id: string;
  readonly title: string;
  readonly subtitle?: string;
  readonly href: string;
  readonly group: PaletteGroup;
}

/** Synonyms an operator is likely to type for a screen that is named otherwise. */
const SCREEN_KEYWORDS: Record<string, string> = {
  Board: "kanban board backlog cards",
  Graph: "dependency dag task graph",
  Architecture: "c4 diagram components layers",
  Org: "organization overview home dashboard",
  Memory: "knowledge rag records recall",
  Monitoring: "metrics cost usage tokens charts",
  Logs: "loki tail errors trace",
  Plugins: "extensions mcp connectors",
  Settings: "preferences profile members api keys providers budgets",
  Conversations: "messages chat channels",
  Deployments: "releases rollout compose",
  Agents: "roster workers roles",
};

/** The §6.13 sub-pages, which the sidebar collapses into one "Settings" entry. */
const SETTINGS_PAGES: readonly { title: string; href: string }[] = [
  { title: "Profile", href: "/settings/profile" },
  { title: "Providers", href: "/settings/providers" },
  { title: "Members", href: "/settings/members" },
  { title: "API keys", href: "/settings/api-keys" },
  { title: "Notifications", href: "/settings/notifications" },
  { title: "Budgets", href: "/settings/budgets" },
  { title: "Appearance", href: "/settings/appearance" },
];

function haystackOf(...parts: (string | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export interface PaletteSources {
  readonly projects: readonly ProjectOut[];
  readonly agents: readonly AgentOut[];
  /** Tasks, already paired with the project they belong to. */
  readonly tasks: readonly { task: TaskOut; project: ProjectOut }[];
}

export function buildItems(sources: PaletteSources): PaletteItem[] {
  const items: PaletteItem[] = [];

  // The sidebar's single "Settings" entry points at /settings/profile — the
  // same href SETTINGS_PAGES lists as "Profile". Emitting both produced two
  // items with the same id, which is a duplicate React key in the results list.
  // The sub-pages are strictly more useful, so the roll-up entry gives way.
  const settingsHrefs = new Set(SETTINGS_PAGES.map((page) => page.href));
  for (const item of orgNavItems) {
    if (settingsHrefs.has(item.href)) {
      continue;
    }
    items.push({
      id: `screen:${item.href}`,
      title: item.title,
      subtitle: "Screen",
      href: item.href,
      group: "Screens",
      haystack: haystackOf(item.title, SCREEN_KEYWORDS[item.title]),
    });
  }
  for (const page of SETTINGS_PAGES) {
    items.push({
      id: `screen:${page.href}`,
      title: `Settings · ${page.title}`,
      subtitle: "Screen",
      href: page.href,
      group: "Screens",
      haystack: haystackOf("Settings", page.title, SCREEN_KEYWORDS["Settings"]),
    });
  }

  for (const project of sources.projects) {
    items.push({
      id: `project:${project.id}`,
      title: project.display_name,
      subtitle: project.name,
      href: `/projects/${project.id}`,
      group: "Projects",
      haystack: haystackOf(project.display_name, project.name, project.status),
    });
    for (const item of projectNavItems(project.id)) {
      items.push({
        id: `screen:${item.href}`,
        title: `${item.title} · ${project.display_name}`,
        subtitle: project.name,
        href: item.href,
        group: "Screens",
        haystack: haystackOf(
          item.title,
          SCREEN_KEYWORDS[item.title],
          project.name,
          project.display_name,
        ),
      });
    }
  }

  for (const agent of sources.agents) {
    items.push({
      id: `agent:${agent.id}`,
      title: agent.name,
      subtitle: `Agent · ${agent.role}`,
      href: `/agents/${agent.id}`,
      group: "Agents",
      haystack: haystackOf(agent.name, agent.role, agent.roster_id, "agent"),
    });
  }

  for (const { task, project } of sources.tasks) {
    items.push({
      id: `task:${task.id}`,
      title: task.title,
      subtitle: `Task · ${project.display_name} · ${task.status}`,
      // The board is where a task is actionable; the graph deep-links the same
      // id but has no edit affordance.
      href: `/projects/${project.id}/board?task=${task.id}`,
      group: "Tasks",
      haystack: haystackOf(task.title, task.status, project.name, "task"),
    });
  }

  return items;
}
