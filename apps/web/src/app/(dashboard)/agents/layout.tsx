/**
 * The /agents layout, and the repo's FIRST parallel route.
 *
 * `@drawer` is a slot: on a client-side navigation to /agents/[agentId] the
 * interceptor at `@drawer/(.)[agentId]` fills it while `children` stays on the
 * grid, so the detail opens as a slide-over. On a hard navigation — a shared
 * link, a refresh, a new tab — there is nothing to intercept, `@drawer` falls
 * back to `default.tsx` (null) and `[agentId]/page.tsx` renders as a full page.
 *
 * Vol 4 §11 writes this path as `agents/@drawer/(.)agents/[agentId]/page.tsx`.
 * That is not what it means: `(.)` matches the slot's OWN level, which is
 * `agents/`, so `(.)agents/[agentId]` would intercept `/agents/agents/[agentId]`
 * — a route that does not exist. `(.)[agentId]` is the same intent, spelled so
 * it resolves.
 *
 * Note this is a genuine departure from how every other overlay in the app
 * works: the board's task Sheet and the graph's node panel are `?task=`/`?node=`
 * search params written with `replaceState` (TITAN-FRT-006). Those are not
 * back-button dismissible; a route-driven drawer is. The trade is that the
 * drawer is now real navigation history, which for a shareable per-agent view
 * is the better behaviour.
 */
export default function AgentsLayout({
  children,
  drawer,
}: {
  children: React.ReactNode;
  drawer: React.ReactNode;
}) {
  return (
    <>
      {children}
      {drawer}
    </>
  );
}
