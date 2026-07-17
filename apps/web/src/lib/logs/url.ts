/**
 * Filters <-> URL (§6.7: "filter bar … all URL-encoded", restorable).
 *
 * Pure string/params work so the round trip can be tested without a router.
 * There is no shared URL-state helper in this app and no nuqs; the two existing
 * patterns are raw History API (board/graph) and `useSearchParams` +
 * `router.replace` (project detail). The screen uses the latter.
 *
 * Everything here is defensive. A URL is user input: it gets typed, truncated,
 * shared in chat and mangled by link unfurlers, so an unknown level or a
 * half-eaten param must degrade to "no filter" rather than throw on mount.
 */
import {
  EMPTY_FILTERS,
  isLevel,
  type Level,
  type LogFilters,
} from "@/lib/logs/filters";

/** `?project=` is FRT-006's cross-screen project param and predates this screen
 *  (the /logs stub already honoured it), so it keeps that spelling rather than
 *  `project_id`. The rest are this screen's own. */
const PARAM = {
  service: "service",
  level: "level",
  project: "project",
  task: "task",
  agent: "agent",
  search: "q",
  regex: "re",
} as const;

/**
 * Guards against a pathological URL becoming a pathological query.
 *
 * 512 matches the backend's own `_MAX_REGEX`. A looser cap here would let the
 * client happily filter its buffer with a 600-character regex while every
 * history request 422'd — a filter that visibly works on screen and silently
 * fetches nothing.
 */
const MAX_VALUE = 512;
const MAX_REPEATS = 16;

function clean(value: string | null): string | null {
  if (value === null) return null;
  // Emptiness is decided on the trimmed value, but the value KEPT is the raw
  // one. Trimming what we keep would rewrite the user's filter behind their
  // back: leading and trailing spaces are significant to a substring search
  // (`" ERROR "` finds the padded column, `"ERROR"` also finds `"ERRORS"`),
  // and inside a regex ` +$` means something entirely different from `+$`.
  if (value.trim().length === 0) return null;
  if (value.length > MAX_VALUE) return null;
  return value;
}

export function parseFilters(params: URLSearchParams): LogFilters {
  const levels = params
    .getAll(PARAM.level)
    .slice(0, MAX_REPEATS)
    .map((l) => l.toLowerCase())
    // An unknown level is dropped, not passed through: the backend 422s on one,
    // so forwarding it would make a shared link fail to load rather than load
    // with one filter ignored.
    .filter(isLevel);

  return {
    services: params
      .getAll(PARAM.service)
      .slice(0, MAX_REPEATS)
      .map((s) => clean(s))
      .filter((s): s is string => s !== null),
    levels: Array.from(new Set<Level>(levels)),
    projectId: clean(params.get(PARAM.project)),
    taskId: clean(params.get(PARAM.task)),
    agentId: clean(params.get(PARAM.agent)),
    search: clean(params.get(PARAM.search)),
    regex: clean(params.get(PARAM.regex)),
  };
}

/**
 * Write filters into a params object, preserving any params we do not own.
 *
 * Takes the existing params so an unrelated one (a future `?theme=`, an
 * analytics tag) is not silently dropped every time someone types in the filter
 * box.
 */
export function writeFilters(
  filters: LogFilters,
  existing?: URLSearchParams,
): URLSearchParams {
  const params = new URLSearchParams(existing);
  for (const key of Object.values(PARAM)) params.delete(key);

  // Values sorted, not just the keys. `params.sort()` orders by NAME and
  // explicitly preserves the relative order of same-name pairs, so
  // `?service=worker&service=api` and `?service=api&service=worker` would
  // otherwise be different strings for the same filter — and since these are
  // sets, clicking two chips in the other order would churn the URL, which
  // re-derives `filters` and wipes the ring.
  for (const s of [...filters.services].sort()) params.append(PARAM.service, s);
  for (const l of [...filters.levels].sort()) params.append(PARAM.level, l);
  if (filters.projectId) params.set(PARAM.project, filters.projectId);
  if (filters.taskId) params.set(PARAM.task, filters.taskId);
  if (filters.agentId) params.set(PARAM.agent, filters.agentId);
  if (filters.search) params.set(PARAM.search, filters.search);
  if (filters.regex) params.set(PARAM.regex, filters.regex);

  // Deterministic order, so an unchanged filter set produces an unchanged
  // string and the screen does not push a history entry for a no-op.
  params.sort();
  return params;
}

/** The querystring for `filters`, "" when nothing is set. */
export function filtersToQuery(
  filters: LogFilters,
  existing?: URLSearchParams,
): string {
  return writeFilters(filters, existing).toString();
}

export function clearFilters(): LogFilters {
  return EMPTY_FILTERS;
}
