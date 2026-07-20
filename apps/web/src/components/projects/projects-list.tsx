"use client";

import { useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Plus, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { agentsByProject, boardStats } from "@/lib/projects/derive";
import {
  useAgents,
  useProjectBoards,
  useProjects,
  type ProjectSort,
} from "@/lib/projects/hooks";
import { usePrimarySearch } from "@/lib/shortcuts/primary-search";
import { cn } from "@/lib/utils";

/**
 * Projects list (Vol 4 §6.2): filterable, sortable rows showing status,
 * progress, agents, CI and last activity.
 *
 * Contract notes that shape this:
 *  - server-side sort is limited to `^-?created_at$`, so "newest/oldest" are the
 *    only true sorts; a name sort would silently only reorder the fetched page,
 *    so it is done client-side over the full (<=200) list and labelled as such.
 *  - `ProjectOut` has no `updated_at`, so "last activity" can only honestly be
 *    the creation time.
 *  - progress/agents are derived (see derive.ts); CI has no source -> "–".
 */
/**
 * `ui/select` is a Radix listbox: it has no notion of an empty-string value, and
 * passing `value=""` to a SelectItem throws. So "no status filter" needs a
 * sentinel that is not a real status, mapped back to `undefined` at the query
 * boundary. `intake`/`released`/`archived` are the only values the backend ever
 * writes, so `__all__` can never collide with one.
 */
const ALL_STATUSES = "__all__";

/**
 * These two strings are shared by the table and the card list on purpose: they
 * are the honest explanation for two columns that cannot show real data, and
 * having one copy each is what stops the mobile view from quietly claiming
 * something the desktop view does not.
 */
const CI_UNKNOWN_TITLE = "No CI signal is exposed by the backend yet";
const CREATED_TITLE =
  "ProjectOut exposes no updated_at — this is the creation time";

export function ProjectsList() {
  const [status, setStatus] = useState<string>(ALL_STATUSES);
  const [sort, setSort] = useState<ProjectSort>("-created_at");
  const [search, setSearch] = useState("");

  // §10: `/` focuses the screen's primary search. Registering the ref is the
  // whole contract — the single document-level listener in lib/shortcuts owns
  // the key handling, text-input suppression and preventDefault.
  const searchRef = useRef<HTMLInputElement>(null);
  usePrimarySearch(searchRef);

  const query = useProjects({
    status: status === ALL_STATUSES ? undefined : status,
    sort,
  });
  const agentsQuery = useAgents();

  const projects = useMemo(() => query.data?.items ?? [], [query.data]);
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return needle
      ? projects.filter(
          (p) =>
            p.display_name.toLowerCase().includes(needle) ||
            p.name.toLowerCase().includes(needle),
        )
      : projects;
  }, [projects, search]);

  const ids = useMemo(() => filtered.map((p) => p.id), [filtered]);
  const boards = useProjectBoards(ids);
  const agentsByPid = useMemo(
    () => agentsByProject(agentsQuery.data?.items ?? []),
    [agentsQuery.data],
  );

  /**
   * One derived view-model feeding BOTH the md+ table and the sub-md card list.
   *
   * `boards` is index-aligned with `filtered` (useProjectBoards is given `ids`
   * in that order), which is why the derivation happens here, once, rather than
   * inside each renderer — the card list would otherwise have to re-derive from
   * the same positional coupling and would break the first time either list
   * gained a sort or a filter the other didn't.
   */
  const rows = useMemo(
    () =>
      filtered.map((project, index) => {
        const stats = boardStats(boards[index]?.data);
        return {
          id: project.id,
          name: project.display_name,
          status: project.status,
          progressPct: stats.progressPct,
          // `null` is "no work to measure", NOT 0 % — Progress renders it as an
          // indeterminate hatch and the label must agree with that, not print
          // a "0%" the data never supported.
          progressLabel:
            stats.progressPct === null ? "—" : `${stats.progressPct}%`,
          blocked: stats.blocked,
          agentCount: agentsByPid.get(project.id)?.total ?? 0,
          createdLabel: new Date(project.created_at).toLocaleDateString(),
        };
      }),
    [filtered, boards, agentsByPid],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Projects</h1>
        <Button asChild size="sm" className="ml-auto">
          <Link href="/projects/new">
            <Plus aria-hidden />
            New project
          </Link>
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search
            aria-hidden
            className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            ref={searchRef}
            id="projects-filter"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter by name…"
            aria-label="Filter projects by name"
            // `max-md:w-full` so the field doesn't sit at a fixed 224 px on a
            // 375 px screen with the two filters wrapping awkwardly beside it.
            className="w-56 pl-7 max-md:w-full"
          />
        </div>
        {/* Both filters were raw `<select className="h-9">`: 36 px tall (under
            §8's 44 px touch minimum), and the only two controls in the app not
            using the ui/select primitive, so they missed the shared focus ring
            too. SelectTrigger carries both. */}
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger aria-label="Filter by status" className="min-w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {/* Only statuses the backend actually writes: `intake` is the column
                default, and `archived`/`released` are what set_project_status
                sets. Offering "Active"/"Paused" would be a filter that can never
                match anything. */}
            <SelectItem value={ALL_STATUSES}>All statuses</SelectItem>
            <SelectItem value="intake">Intake</SelectItem>
            <SelectItem value="released">Released</SelectItem>
            <SelectItem value="archived">Archived</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={sort}
          onValueChange={(value) => setSort(value as ProjectSort)}
        >
          <SelectTrigger aria-label="Sort projects" className="min-w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="-created_at">Newest first</SelectItem>
            <SelectItem value="created_at">Oldest first</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {query.isLoading ? (
        <ul className="divide-y rounded-lg border bg-card">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex items-center gap-4 p-3">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-1.5 w-32" />
              <Skeleton className="ml-auto h-4 w-24" />
            </li>
          ))}
        </ul>
      ) : query.isError ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-destructive">
          Couldn’t load projects.{" "}
          <button
            type="button"
            className="underline underline-offset-4"
            onClick={() => void query.refetch()}
          >
            Retry
          </button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            {projects.length === 0
              ? "No projects yet — describe one and the team starts planning."
              : "No projects match this filter."}
          </p>
          {projects.length === 0 ? (
            <Button asChild size="sm">
              <Link href="/projects/new">Start a project</Link>
            </Button>
          ) : null}
        </div>
      ) : (
        <>
          {/*
            TITAN-FRT-043 ("tables as tables") + §8 ("< 768: tables → card
            lists"). This was one `<ul>` of flex rows faking six columns with no
            headers anywhere: a screen reader read "0 agents", "CI –",
            "7/19/2026" as a flat run of text with nothing to say which column
            any of it belonged to, and no way to navigate cell-by-cell. A real
            <table> at md+ gives every cell a <th> association for free.

            Below md the SAME rows render as cards instead, because a six-column
            table at 375 px is either a horizontal-scroll trap or unreadably
            cramped. Both branches read from `rows` below, computed once — two
            hand-maintained copies of the same six fields is exactly how the
            mobile view silently drifts out of date.
          */}
          <div className="hidden md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Project</TableHead>
                  <TableHead scope="col">Status</TableHead>
                  <TableHead scope="col">Progress</TableHead>
                  <TableHead scope="col">Agents</TableHead>
                  <TableHead scope="col">CI</TableHead>
                  <TableHead scope="col" className="text-right">
                    Created
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.id}>
                    {/*
                      The link lives in the row-header cell, not on the <tr>: an
                      <a> cannot wrap <td>s, and an onClick on the <tr> would be
                      a click target no keyboard or screen reader can reach.
                      `scope="row"` then makes the project name the label every
                      other cell in the row is announced against.
                    */}
                    <TableHead scope="row" className="font-medium">
                      <Link
                        href={`/projects/${row.id}`}
                        className={cn(
                          "block truncate rounded-sm text-foreground hover:underline",
                          FOCUS_RING,
                        )}
                      >
                        {row.name}
                      </Link>
                    </TableHead>
                    <TableCell>
                      <Badge variant="outline" className="capitalize">
                        {row.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="flex items-center gap-2">
                        <Progress
                          label={`${row.name} progress`}
                          value={row.progressPct}
                          className="w-16"
                        />
                        <span className="text-xs tabular-nums text-muted-foreground">
                          {row.progressLabel}
                        </span>
                      </span>
                      {row.blocked > 0 ? (
                        <span className="text-xs font-medium text-warning">
                          {row.blocked} blocked
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-muted-foreground tabular-nums">
                      {row.agentCount}
                    </TableCell>
                    <TableCell
                      className="text-muted-foreground"
                      title={CI_UNKNOWN_TITLE}
                    >
                      –
                    </TableCell>
                    <TableCell
                      className="text-right text-muted-foreground"
                      title={CREATED_TITLE}
                    >
                      {row.createdLabel}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <ul className="divide-y rounded-lg border bg-card md:hidden">
            {rows.map((row) => (
              <li key={row.id}>
                <Link
                  href={`/projects/${row.id}`}
                  // §8: `min-h-11` — the whole card is the touch target, and at
                  // 375 px it is the ONLY way into a project.
                  className={cn(
                    "flex min-h-11 flex-col gap-2 rounded-lg p-3 hover:bg-accent/40",
                    FOCUS_RING,
                  )}
                >
                  <span className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-medium">
                      {row.name}
                    </span>
                    <Badge variant="outline" className="capitalize">
                      {row.status}
                    </Badge>
                  </span>
                  <span className="flex items-center gap-2">
                    <Progress
                      label={`${row.name} progress`}
                      value={row.progressPct}
                      className="w-20"
                    />
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {row.progressLabel}
                    </span>
                    {row.blocked > 0 ? (
                      <span className="text-xs font-medium text-warning">
                        {row.blocked} blocked
                      </span>
                    ) : null}
                  </span>
                  {/*
                    Card layout has no column headers to inherit meaning from,
                    so each value carries its own label inline — this is the
                    same information the <th>s supply at md+, not decoration.
                  */}
                  <span className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
                    <span>
                      {row.agentCount}{" "}
                      {row.agentCount === 1 ? "agent" : "agents"}
                    </span>
                    <span title={CI_UNKNOWN_TITLE}>CI –</span>
                    <span title={CREATED_TITLE}>
                      Created {row.createdLabel}
                    </span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
