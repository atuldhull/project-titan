"use client";

/**
 * The filter bar (§6.7). Every control writes to the URL; the screen owns that.
 */
import { useEffect, useRef, useState } from "react";
import { Download, Pause, Play, Search, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LEVELS, type Level, type LogFilters } from "@/lib/logs/filters";

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
}

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
}: LogFilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b p-2">
      <MultiSelect
        label="Service"
        options={[...SERVICES]}
        selected={filters.services}
        onChange={(services) => onChange({ ...filters, services })}
        disabled={disabled}
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

      <DebouncedInput
        value={filters.search}
        onCommit={(search) => onChange({ ...filters, search })}
        placeholder="Search (case-sensitive)"
        icon
        disabled={disabled}
      />
      <div className="flex flex-col">
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
          className="gap-1"
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
}: {
  label: string;
  options: string[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
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
            className="h-6 px-2 text-[11px]"
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
}: {
  value: string | null;
  onCommit: (next: string | null) => void;
  placeholder: string;
  icon?: boolean;
  invalid?: boolean;
  disabled?: boolean;
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
    <div className="relative">
      {icon ? (
        <Search
          className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
      ) : null}
      <Input
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        aria-invalid={invalid}
        className={`h-7 w-44 font-mono text-xs ${icon ? "pl-7" : "pl-2"} ${
          invalid ? "border-destructive" : ""
        }`}
      />
      {draft ? (
        <button
          type="button"
          onClick={() => setDraft("")}
          aria-label={`Clear ${placeholder}`}
          className="absolute right-1 top-1/2 -translate-y-1/2 rounded text-muted-foreground hover:text-foreground"
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
