"use client";

/**
 * The filter bar (§6.7). Every control writes to the URL; the screen owns that.
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { Download, Pause, Play, Search, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LEVELS, type Level, type LogFilters } from "@/lib/logs/filters";
import { cn } from "@/lib/utils";

/**
 * The Compose services that emit Titan's structured JSON. Only four of the
 * sixteen containers do: `web` is Next.js, and postgres/redis/qdrant/grafana
 * and friends ship their own plain-text formats through the same promtail.
 *
 * §6.7's control is "Stream: agents ▾" over agents/system/build. That taxonomy
 * has nothing behind it: the only low-cardinality label is `service`, agent work
 * and API work are not separable by any label (agents run inside `worker`), and
 * generated-app build output is not in Loki AT ALL — promtail only scrapes
 * containers labelled `com.docker.compose.project=titan`, and sandbox
 * containers are created through the Docker API, not Compose. So the control
 * offers the real label, with the real values.
 */
const SERVICES = [
  "api",
  "worker",
  "titan-egress-proxy",
  "titan-egress-gateway",
] as const;

export interface LogFilterBarProps {
  readonly filters: LogFilters;
  readonly onChange: (next: LogFilters) => void;
  readonly tailing: boolean;
  readonly onToggleTail: () => void;
  readonly wrap: boolean;
  readonly onToggleWrap: () => void;
  readonly onExport: () => void;
  readonly shown: number;
  readonly regexError: string | null;
  readonly disabled?: boolean;
  /** §10's `/` target — see `usePrimarySearch` in `logs-screen`. */
  readonly searchRef?: RefObject<HTMLInputElement | null>;
}

/**
 * §8, under 768: "Logs keeps virtualized list with **simplified filters**".
 *
 * Simplified means fewer controls on screen, not fewer controls available. Four
 * services + five levels + two text boxes + three buttons is eleven rows of
 * wrapped chrome at 375px, above a list that is the entire point of the screen.
 *
 * The split below keeps what a phone actually filters with — free-text search,
 * level, and the tail toggle — and folds away Service, the regex box and Export.
 *
 * Folding is conditional on the filter being EMPTY, which is the part a plain
 * `hidden md:flex` gets wrong: `/logs?service=worker` is a shareable link, and
 * hiding the control that set it would leave a filter the reader can see the
 * effect of, cannot see the cause of, and cannot clear without editing the URL.
 * An ACTIVE fold-away filter therefore stays on screen at every width.
 */
export function LogFilterBar({
  filters,
  onChange,
  tailing,
  onToggleTail,
  wrap,
  onToggleWrap,
  onExport,
  shown,
  regexError,
  disabled,
  searchRef,
}: LogFilterBarProps) {
  const serviceActive = filters.services.length > 0;
  // `regexError` counts as active: a rejected pattern is exactly when the box
  // must be reachable, and the pattern that produced it may have come from a
  // link rather than from typing.
  const regexActive = Boolean(filters.regex) || regexError !== null;

  return (
    <div className="flex flex-wrap items-center gap-2 border-b p-2">
      <DebouncedInput
        inputRef={searchRef}
        value={filters.search}
        onCommit={(search) => onChange({ ...filters, search })}
        placeholder="Search (case-sensitive)"
        icon
        disabled={disabled}
        // Full width under md so it is not competing with the level chips for a
        // 375px line; the fixed desktop width comes back at md+.
        containerClassName="w-full md:w-44"
      />

      <MultiSelect
        label="Level"
        options={[...LEVELS]}
        selected={filters.levels}
        onChange={(levels) =>
          onChange({ ...filters, levels: levels as Level[] })
        }
        disabled={disabled}
      />
      <MultiSelect
        label="Service"
        options={[...SERVICES]}
        selected={filters.services}
        onChange={(services) => onChange({ ...filters, services })}
        disabled={disabled}
        className={cn(!serviceActive && "hidden md:flex")}
      />

      <div className={cn("flex flex-col", !regexActive && "hidden md:flex")}>
        <DebouncedInput
          value={filters.regex}
          onCommit={(regex) => onChange({ ...filters, regex })}
          placeholder="/regex/"
          invalid={regexError !== null}
          disabled={disabled}
        />
        {regexError ? (
          <span className="px-1 text-[10px] text-destructive" role="status">
            {regexError}
          </span>
        ) : null}
      </div>

      <div className="ml-auto flex items-center gap-2">
        <span className="text-xs text-muted-foreground tabular-nums">
          {shown.toLocaleString()} lines
        </span>
        <Button
          type="button"
          size="sm"
          variant={tailing ? "default" : "outline"}
          onClick={onToggleTail}
          className="gap-1"
          // A pressed state cannot ride on the variant alone: aria-pressed is
          // what a screen reader reads, and the icon is what a greyscale
          // reader sees (TITAN-FRT-044).
          aria-pressed={tailing}
        >
          {tailing ? (
            <Pause className="h-3 w-3" aria-hidden />
          ) : (
            <Play className="h-3 w-3" aria-hidden />
          )}
          {tailing ? "Tailing" : "Paused"}
        </Button>
        {/* Wrap survives the fold precisely BECAUSE the screen is narrow: it is
            the control that turns a truncated line into a readable one. */}
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onToggleWrap}
          aria-pressed={wrap}
        >
          Wrap
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onExport}
          className="hidden gap-1 md:inline-flex"
        >
          <Download className="h-3 w-3" aria-hidden />
          Export
        </Button>
      </div>
    </div>
  );
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
  disabled,
  className,
}: {
  label: string;
  options: string[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    // Scrolls in its own container under md rather than wrapping (§8: "no
    // horizontal body scroll ever; wide content scrolls in its own container").
    // `service` in particular holds `titan-egress-gateway`, which alone is wider
    // than half a 375px screen.
    //
    // `max-md:-my-1 max-md:py-1` is the focus ring's room, not spacing: an
    // `overflow-x` that is not `visible` makes the unset `overflow-y` compute to
    // `auto` too, and this box is exactly as tall as its 44px chips — so every
    // chip's ring would be clipped flat top and bottom (TITAN-FRT-042). The
    // negative margin hands the padding back so no layout moves.
    <div
      className={cn(
        "-mx-1 flex max-w-full items-center gap-1 overflow-x-auto px-1 max-md:-my-1 max-md:py-1 md:mx-0 md:overflow-x-visible md:px-0",
        className,
      )}
    >
      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
      {options.map((option) => {
        const active = selected.includes(option);
        return (
          <Button
            key={option}
            type="button"
            size="sm"
            variant={active ? "secondary" : "ghost"}
            disabled={disabled}
            aria-pressed={active}
            // `Button` already contributes `max-md:min-h-11` (§8), so only the
            // WIDTH needs help: `api` is ~22px of text and `px-2` leaves it a
            // 38px-wide target. `max-md:px-3` clears 44 without touching the
            // dense ≥md chip.
            className="h-6 shrink-0 px-2 text-[11px] max-md:px-3"
            onClick={() =>
              onChange(
                active
                  ? selected.filter((s) => s !== option)
                  : [...selected, option],
              )
            }
          >
            {option}
          </Button>
        );
      })}
    </div>
  );
}

/**
 * A text filter that commits after a pause.
 *
 * Not cosmetic: a regex re-filters the whole ring buffer, which measures ~26ms
 * against a saturated 50k lines — inside a 30fps frame but over a 60fps one. Per
 * keystroke that is a visibly janky input, and it would also fire a backend
 * query per character. Committing on a pause makes both problems go away.
 */
const DEBOUNCE_MS = 200;

function DebouncedInput({
  value,
  onCommit,
  placeholder,
  icon,
  invalid,
  disabled,
  containerClassName,
  inputRef,
}: {
  value: string | null;
  onCommit: (next: string | null) => void;
  placeholder: string;
  icon?: boolean;
  invalid?: boolean;
  disabled?: boolean;
  containerClassName?: string;
  inputRef?: RefObject<HTMLInputElement | null>;
}) {
  const [draft, setDraft] = useState(value ?? "");

  // Re-sync when the filter changes from outside (a reset, a shared link, the
  // back button). Keyed on `value` alone: adding `draft` would fight the user's
  // typing.
  useEffect(() => {
    setDraft(value ?? "");
  }, [value]);

  // `onCommit` closes over the CURRENT filters, and the screen re-creates it
  // whenever they change. Putting it in the dep array below would restart the
  // debounce on every one of those renders, so it would never fire; leaving it
  // out and calling it directly is worse — the timer would invoke the callback
  // captured when typing started, whose `filters` snapshot predates any filter
  // the user changed in the meantime, and `{...filters, search}` would write
  // that stale snapshot back, silently undoing them. A ref keeps the timer
  // stable AND the callback current.
  const commitRef = useRef(onCommit);
  commitRef.current = onCommit;

  useEffect(() => {
    const next = draft.trim() === "" ? null : draft;
    if (next === value) return;
    const handle = setTimeout(() => commitRef.current(next), DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [draft, value]);

  return (
    <div className={cn("relative w-44", containerClassName)}>
      {icon ? (
        <Search
          className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
      ) : null}
      <Input
        ref={inputRef}
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        aria-invalid={invalid}
        // Width now comes from the container, so `containerClassName` can make
        // this full-bleed under md without a second class to keep in sync. The
        // 44px touch height is NOT set here: `Input` already carries
        // `TOUCH_TARGET` (`max-md:min-h-11`), which lifts this `h-7` under md
        // on its own. Restating it would just fight tailwind-merge.
        className={cn(
          "h-7 w-full font-mono text-xs",
          icon ? "pl-7" : "pl-2",
          invalid && "border-destructive",
        )}
      />
      {draft ? (
        <button
          type="button"
          onClick={() => setDraft("")}
          aria-label={`Clear ${placeholder}`}
          // A raw <button>, so it gets none of the primitives' touch sizing:
          // an 12px ✕ is a 12px tap target. Squared off to 44 under md (§8).
          className="absolute right-1 top-1/2 flex -translate-y-1/2 items-center justify-center rounded text-muted-foreground hover:text-foreground max-md:size-11"
        >
          <X className="h-3 w-3" />
        </button>
      ) : null}
    </div>
  );
}

/** The active-filter summary the empty state offers to reset. */
export function FilterSummary({ filters }: { filters: LogFilters }) {
  const chips: string[] = [
    ...filters.services.map((s) => `service=${s}`),
    ...filters.levels.map((l) => `level=${l}`),
    filters.projectId ? `project=${filters.projectId}` : null,
    filters.taskId ? `task=${filters.taskId}` : null,
    filters.agentId ? `agent=${filters.agentId}` : null,
    filters.search ? `search=${filters.search}` : null,
    filters.regex ? `regex=${filters.regex}` : null,
  ].filter((c): c is string => c !== null);

  return (
    <div className="flex flex-wrap justify-center gap-1">
      {chips.map((chip) => (
        <Badge key={chip} variant="secondary" className="font-mono text-[10px]">
          {chip}
        </Badge>
      ))}
    </div>
  );
}
