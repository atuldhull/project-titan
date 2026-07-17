/**
 * The `@drawer` slot's fallback: nothing.
 *
 * Required, not optional. Without a `default.tsx` a hard navigation to
 * /agents/[agentId] — which does not match the interceptor — leaves the slot
 * unresolvable and Next.js 404s the whole page rather than rendering the
 * full-page detail. This is what makes a shared agent link work.
 */
export default function NoDrawer() {
  return null;
}
