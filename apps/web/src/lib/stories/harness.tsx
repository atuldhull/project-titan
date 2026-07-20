"use client";

/**
 * The screen-story harness (TITAN-FRT-002).
 *
 * §1.4 requires every screen to "define and render distinct loading, empty,
 * error, and populated states", with a Storybook story per state per screen.
 * Until Phase 66 no story mounted a screen at all: the three that came closest
 * (Kanban, Graph, Logs) rendered extracted presentational pieces and said so,
 * because a screen fetches its own data and holds a socket.
 *
 * That workaround is what this replaces. Rendering the pieces proves the pieces
 * exist; it cannot prove the SCREEN reaches them, and the four defects Phase 66
 * found were all of exactly that kind — /deployments had an empty-state
 * component and no branch that could render it. So the harness drives the real
 * screen through the real hooks, and the only thing it fakes is the network.
 *
 * **How a state is produced.** One `fetch` stub, chosen by a story parameter:
 *
 * - `loading`   — a promise that never settles, so every query stays pending.
 * - `empty`     — the populated fixture with its collections emptied, or the
 *                 rule's own `emptyBody` where emptying by shape would lie.
 * - `error`     — 500 + problem+json, with React Query retries off so the
 *                 error branch renders immediately rather than after a backoff.
 * - `populated` — the shared fixture corpus in `lib/fixtures/api`, the same one
 *                 the Playwright suite serves, typed through the generated
 *                 OpenAPI `paths`.
 *
 * Sharing that corpus with E2E is deliberate. A story fixture invented beside
 * the story it feeds will agree with whatever the screen currently does; one
 * that has to satisfy `GetOk<P>` agrees with the contract instead. An earlier
 * phase shipped 22 green tests against a fixture that omitted a constraint the
 * real schema had, and this is the cheapest guard against repeating it.
 *
 * **Unmatched requests fail loudly.** A path with no fixture answers 599, not
 * an empty body — otherwise a screen renders its error state, the story still
 * looks like a screen, and the missing fixture is invisible. Same discipline as
 * `e2e/support/fixtures.ts`.
 */

import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import { ShortcutProvider } from "@/lib/shortcuts/provider";
import { SessionProvider } from "@/lib/auth/session-context";
import { API_FIXTURES, type FixtureRule } from "@/lib/fixtures/api";
import type { Session } from "@/lib/auth/types";

/**
 * The browser's own `fetch`, captured at module load — before any story has had
 * a chance to replace it. Every stub falls back to this one, so passthrough
 * requests never walk a chain of previous stories' stubs.
 */
const PRISTINE_FETCH: typeof fetch = globalThis.fetch.bind(globalThis);

export type ScreenState = "loading" | "empty" | "error" | "populated";

/** The four states TITAN-FRT-002 names, in the order the stories declare them. */
export const SCREEN_STATES: readonly ScreenState[] = [
  "loading",
  "empty",
  "error",
  "populated",
];

/**
 * An owner session, so a populated story renders the full screen.
 *
 * Not a detail: `useCan` returns false for a null session, so without this
 * every screen would story its read-only variant — every mutating control
 * hidden — and the populated snapshot would be of a UI no owner ever sees.
 */
const STORY_SESSION: Session = {
  sub: "00000000-0000-4000-8000-0000000000e2",
  org: "00000000-0000-4000-8000-00000000006f",
  roles: ["owner"],
  scope: "org",
  kind: "user",
  expiresAt: Math.floor(Date.UTC(2099, 0, 1) / 1000),
};

/**
 * Empty a fixture body without inventing one.
 *
 * Collections become empty; scalars are left alone. Deriving the empty state
 * from the populated fixture rather than hand-writing a second corpus means the
 * two cannot drift, and an empty story stays valid when a field is added.
 *
 * The limit of the heuristic, and why `FixtureRule.emptyBody` exists: it works
 * on SHAPE, so it cannot see that one field summarises another. `/usage` returns
 * a `total` roll-up beside the `groups` it sums, and emptying the groups left
 * the totals untouched — §6.10's empty story showed a populated KPI row above
 * "No data in range". A rule that owns such a relationship states its empty body
 * outright; the other ~30 fixtures keep this default.
 */
function emptied(body: unknown): unknown {
  if (Array.isArray(body)) {
    return [];
  }
  if (body !== null && typeof body === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(body)) {
      out[key] = Array.isArray(value) ? [] : emptied(value);
    }
    return out;
  }
  return body;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function problemResponse(status: number, detail: string): Response {
  return new Response(
    JSON.stringify({ title: "Story fixture", status, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/**
 * The stub installed for the duration of one story.
 *
 * Anything that is not a BFF call (Next's own chunk and RSC requests) passes
 * through to the real fetch — stubbing those would break Storybook itself.
 */
export function storyFetch(
  state: ScreenState,
  overrides: readonly FixtureRule[],
  real: typeof fetch,
): typeof fetch {
  const rules = [...overrides, ...API_FIXTURES];

  return async function stub(input, init) {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const path = new URL(url, "http://localhost").pathname;

    if (!path.startsWith("/api/titan")) {
      return real(input, init);
    }
    const apiPath = path.replace(/^\/api\/titan/, "");
    const method = (init?.method ?? "GET").toUpperCase();

    if (state === "loading") {
      // Never settles: React Query stays pending, which is the loading state as
      // the screen actually computes it. A slow-but-finite delay would race the
      // snapshot instead.
      return new Promise<Response>(() => {});
    }
    if (state === "error") {
      return problemResponse(500, `${method} ${apiPath}`);
    }

    const rule = rules.find(
      (candidate) =>
        candidate.pattern.test(apiPath) &&
        (candidate.method ?? "GET") === method,
    );
    if (!rule) {
      return problemResponse(
        599,
        `No story fixture for ${method} ${apiPath} — add one to lib/fixtures/api.ts`,
      );
    }
    const body =
      state === "empty" ? (rule.emptyBody ?? emptied(rule.body)) : rule.body;
    return jsonResponse(body, rule.status ?? 200);
  };
}

/**
 * A client per story.
 *
 * `getQueryClient()` is a browser singleton, which is right for the app and
 * wrong here: stories would inherit each other's cache, so whether an "empty"
 * story rendered empty would depend on which story ran before it. `retry: false`
 * matters just as much — the app retries once, and a story that waits out a
 * backoff before showing its error state is a story that screenshots a
 * skeleton.
 */
function storyQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Infinity,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export function ScreenHarness({
  state,
  overrides = [],
  children,
}: {
  readonly state: ScreenState;
  readonly overrides?: readonly FixtureRule[];
  readonly children: React.ReactNode;
}) {
  // `useState` initialiser, not a bare call: a new client on every re-render
  // would discard in-flight queries and the story would never settle.
  const [client] = useState(storyQueryClient);

  // Installed during RENDER, deliberately. Queries fire from the children's
  // effects, and a parent's effect runs after its children's — so installing
  // this in `useEffect` would leave the first request of every story hitting
  // the real network.
  //
  // The fallback is the module-level PRISTINE_FETCH rather than whatever
  // `globalThis.fetch` happens to be right now. Capturing it here instead
  // chained each story's stub onto the previous story's, because Storybook
  // swaps stories in one long-lived page and nothing ever put the original
  // back: 52 stories deep, a passthrough request would walk 52 wrappers, and
  // the "original" any of them would restore was already a stub.
  useState(() => {
    globalThis.fetch = storyFetch(state, overrides, PRISTINE_FETCH);
    return null;
  });
  useEffect(
    () => () => {
      globalThis.fetch = PRISTINE_FETCH;
    },
    [],
  );

  return (
    <QueryClientProvider client={client}>
      <SessionProvider session={STORY_SESSION}>
        {/*
         * No `WsProvider`. `useWsClient()` is nullable by design and every
         * `useTopic` call already returns early on a null client, so screens
         * degrade to their no-socket behaviour — which is what a story wants.
         * Mounting the real provider would open a socket to a backend that is
         * not there and put a reconnect backoff under every snapshot.
         */}
        {/*
         * `ShortcutProvider` is not optional: `useShortcuts` THROWS without it,
         * the same way `Tooltip` throws without `TooltipProvider`. Three screens
         * register shortcuts (Logs, the Kanban board, Conversations) and all
         * three crashed on mount until this was added — the identical failure
         * mode that took six routes down in Phase 65 when `TooltipProvider` was
         * mounted on one screen instead of in the layout. A provider that throws
         * rather than degrades is invisible until something renders outside the
         * layout that supplies it, and stories are the first thing that does.
         */}
        <ShortcutProvider>
          <TooltipProvider>{children}</TooltipProvider>
        </ShortcutProvider>
      </SessionProvider>
    </QueryClientProvider>
  );
}
