"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Search } from "lucide-react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { rank } from "@/lib/palette/search";
import {
  buildItems,
  type PaletteGroup,
  type PaletteItem,
} from "@/lib/palette/items";
import {
  usePaletteCandidates,
  TASK_FANOUT_PROJECTS,
} from "@/lib/palette/use-candidates";
import { usePaletteStore, type PaletteMode } from "@/stores/palette";

/**
 * The command palette (⌘K) and project switcher (⌘P) — TITAN-FRT-007, §2.3, §10.
 *
 * Hand-rolled on `ui/dialog` rather than pulling in `cmdk`: the palette mounts
 * in the shell, so it lands in the `/org` First Load JS that TITAN-FRT-005 caps
 * at 300 kB, and the accessible combobox pattern here is ~80 lines.
 *
 * The pattern is APG combobox-with-listbox: focus never leaves the input, and
 * the active option is pointed at with `aria-activedescendant`. Moving DOM focus
 * onto the options instead would fight the Dialog's focus trap and break
 * type-ahead, which is the whole interaction.
 */

/** Rendering cap. Matching is not capped — this only bounds the DOM. */
const MAX_RENDERED = 50;

const GROUP_ORDER: readonly PaletteGroup[] = [
  "Screens",
  "Projects",
  "Agents",
  "Tasks",
];

export function CommandPalette() {
  const router = useRouter();
  const mode = usePaletteStore((state) => state.mode);
  const close = usePaletteStore((state) => state.close);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const open = mode !== null;
  // ⌘P shows projects only, so the task fan-out would issue up to eight
  // requests whose results are then filtered straight back out.
  const candidates = usePaletteCandidates(open, query, mode !== "projects");

  const items = useMemo(() => {
    const all = buildItems(candidates);
    // ⌘P is the same machinery narrowed to one group (§2.3 "project switcher").
    return mode === "projects"
      ? all.filter((item) => item.group === "Projects")
      : all;
  }, [candidates, mode]);

  const results = useMemo(() => {
    const ranked = rank(query, items);
    // With no query, group order gives a stable, browsable list; with a query,
    // relevance order is the point and regrouping would destroy it.
    if (query.trim() === "") {
      return [...ranked].sort(
        (a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group),
      );
    }
    return ranked;
  }, [items, query]);

  const shown = results.slice(0, MAX_RENDERED);

  useEffect(() => {
    if (!open) {
      setQuery("");
    }
  }, [open]);

  // Any change to the result set invalidates the highlight: leaving it where it
  // was means Enter navigates somewhere the user is no longer looking at.
  useEffect(() => {
    setActive(0);
  }, [query, mode]);

  // Task results arrive asynchronously (the fan-out in `usePaletteCandidates`),
  // so the list can SHRINK under a highlight that was valid a moment ago —
  // after which Enter silently does nothing.
  useEffect(() => {
    setActive((current) =>
      current >= shown.length ? Math.max(0, shown.length - 1) : current,
    );
  }, [shown.length]);

  useEffect(() => {
    listRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [active, query]);

  const go = (item: PaletteItem | undefined) => {
    if (!item) {
      return;
    }
    close();
    router.push(item.href);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (shown.length ? (current + 1) % shown.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) =>
        shown.length ? (current - 1 + shown.length) % shown.length : 0,
      );
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(Math.max(0, shown.length - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(shown[active]);
    }
  };

  const label = mode === "projects" ? "Switch project" : "Command palette";
  const placeholder =
    mode === "projects"
      ? "Find a project…"
      : "Search screens, projects, agents and tasks…";

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          close();
        }
      }}
    >
      <DialogContent
        showClose={false}
        aria-describedby={undefined}
        className="top-[12%] max-h-[min(30rem,calc(100dvh-6rem))] translate-y-0 gap-0 overflow-hidden p-0"
      >
        <DialogTitle className="sr-only">{label}</DialogTitle>
        <div className="flex items-center gap-2 border-b px-3">
          <Search
            aria-hidden
            className="size-4 shrink-0 text-muted-foreground"
          />
          <input
            autoFocus
            type="text"
            role="combobox"
            aria-expanded
            aria-controls="palette-results"
            aria-label={label}
            aria-activedescendant={
              shown.length > 0 ? `palette-option-${active}` : undefined
            }
            autoComplete="off"
            value={query}
            placeholder={placeholder}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>

        <ul
          ref={listRef}
          id="palette-results"
          role="listbox"
          aria-label={`${label} results`}
          className="max-h-80 overflow-y-auto overscroll-contain p-1"
        >
          {/*
           * The <li> IS the option. Wrapping a role="option" div inside a
           * role-less <li> puts a non-option element between the listbox and
           * its options, which breaks the required owned-element relationship
           * (axe: aria-required-children) — the listbox stops being a listbox
           * for assistive tech even though it still looks right.
           */}
          {shown.map((item, index) => {
            const previous = shown[index - 1];
            const heading =
              query.trim() === "" && previous?.group !== item.group
                ? item.group
                : null;
            return (
              <Fragment key={item.id}>
                {heading && (
                  <li
                    role="presentation"
                    className="px-2 pt-3 pb-1 text-xs font-medium text-muted-foreground"
                  >
                    {heading}
                  </li>
                )}
                <li
                  id={`palette-option-${index}`}
                  role="option"
                  aria-selected={index === active}
                  data-active={index === active}
                  onClick={() => go(item)}
                  onMouseMove={() => setActive(index)}
                  className={cn(
                    "flex min-h-11 cursor-pointer items-center justify-between gap-3 rounded-md px-2 py-2 text-sm",
                    index === active && "bg-accent text-foreground",
                  )}
                >
                  <span className="truncate">{item.title}</span>
                  {item.subtitle && (
                    <span className="shrink-0 truncate text-xs text-muted-foreground">
                      {item.subtitle}
                    </span>
                  )}
                </li>
              </Fragment>
            );
          })}
        </ul>

        {/* A result count that is a live region: the listbox itself must not be
            one, or every keystroke re-announces the whole list. */}
        <div
          aria-live="polite"
          className="flex items-center justify-between gap-3 border-t px-3 py-2 text-xs text-muted-foreground"
        >
          <span>
            {shown.length > 0
              ? `${shown.length} of ${results.length} shown`
              : candidates.tasksLoading
                ? "Searching…"
                : candidates.tasksFailed
                  ? // A failed search is not an empty one: "No matches" would
                    // tell the operator their task does not exist.
                    "Couldn’t search tasks — screens and projects only"
                  : "No matches"}
          </span>
          {/*
           * The bound is disclosed, not hidden. There is no global task list
           * endpoint, so task search fans out over the most recent projects
           * only — presenting that as a complete index would be a lie the user
           * has no way to detect.
           */}
          {candidates.tasksTruncated && !candidates.tasksFailed && (
            <span>Tasks: {TASK_FANOUT_PROJECTS} most recent projects</span>
          )}
          {!candidates.tasksTruncated && results.length > MAX_RENDERED && (
            <span>Keep typing to narrow the list</span>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Opens the palette in a given mode — used by the topbar's two triggers. */
export function usePaletteOpener(): (mode: PaletteMode) => void {
  return usePaletteStore((state) => state.open);
}
