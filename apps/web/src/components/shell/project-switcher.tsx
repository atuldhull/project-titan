"use client";

import { usePathname } from "next/navigation";
import { ChevronsUpDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useProject } from "@/lib/projects/hooks";

/**
 * The top bar's project switcher (§2.3). Opens the palette in `projects` mode —
 * ⌘P and this button are the same surface, so there is one search behaviour to
 * learn and one to test.
 *
 * The label resolves the id in the URL to a display name via `useProject`, which
 * is the SAME cache entry the project detail screen fills: on any project screen
 * this costs nothing, and off one it does not fetch at all.
 */
export function ProjectSwitcherButton({ onOpen }: { onOpen: () => void }) {
  const pathname = usePathname();
  const pid = /^\/projects\/([^/]+)/.exec(pathname)?.[1];
  // `new` is the intake route, not a project id — asking the API for it 404s.
  const project = useProject(pid && pid !== "new" ? pid : "");

  const label = project.data?.display_name ?? (pid ? "Loading…" : "No project");

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={onOpen}
      aria-label={`Switch project. Current: ${label}`}
      aria-keyshortcuts="Control+P Meta+P"
      className="max-w-44 justify-between gap-2 md:min-w-44"
    >
      <span className="truncate text-muted-foreground">{label}</span>
      <ChevronsUpDown
        aria-hidden
        className="size-3.5 shrink-0 text-muted-foreground"
      />
    </Button>
  );
}
