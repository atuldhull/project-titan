"use client";

import { create } from "zustand";

/**
 * The assertive announcement channel for critical alerts (TITAN-FRT-043:
 * "toasts polite, critical alerts assertive").
 *
 * WHY THIS EXISTS AT ALL — sonner cannot do it. Verified against the installed
 * sonner 1.7.4 (node_modules/sonner/dist): its Toaster renders ONE
 * `<section aria-live="polite" aria-relevant="additions text" aria-atomic="false">`
 * around the whole stack, hardcoded. The string "assertive" does not appear
 * anywhere in the package, `ToastT`/`ExternalToast` expose no `important` field
 * (it was never a sonner option — that is a react-hot-toast/notistack idiom),
 * and there is no prop that changes the container's politeness. So passing an
 * `important` flag through `toast(...)` would be a TypeScript excess-property
 * error that, even if it compiled, would change nothing at runtime: every
 * critical alert would still be announced politely and could sit silently
 * behind an in-progress utterance while a release is blocked or a plugin is
 * quarantined.
 *
 * Rather than fork the toast library, the ASSERTIVE half of the requirement is
 * served by our own region: `showNotificationToast` still renders the toast
 * (visual + polite, from sonner), and additionally pushes critical text here,
 * which <CriticalAnnouncer> mirrors into an `aria-live="assertive"` sr-only
 * region. Two channels, one for each half of the requirement.
 *
 * `seq` is not decoration. A live region only announces when its content
 * MUTATES, so two identical consecutive alerts ("Approval required") would be
 * announced once and the second would be lost — precisely the case where
 * silence is most expensive. The renderer keys on `seq`, so an identical
 * message still remounts the node and still speaks.
 */
export interface CriticalAnnouncement {
  readonly seq: number;
  readonly text: string;
}

interface AnnounceState {
  latest: CriticalAnnouncement | null;
  announce: (text: string) => void;
  /** Test seam — resets the sequence so assertions aren't order-dependent. */
  reset: () => void;
}

export const useAnnounceStore = create<AnnounceState>((set) => ({
  latest: null,
  announce: (text) =>
    set((state) => ({
      latest: { seq: (state.latest?.seq ?? 0) + 1, text },
    })),
  reset: () => set({ latest: null }),
}));

/**
 * Queue an assertive announcement. Called from the notification path only —
 * assertive interrupts whatever the user is currently listening to, so it is
 * reserved for `severity === "critical"` and must not be widened to "high" or
 * to ordinary progress chatter without re-reading §9.
 */
export function announceCritical(text: string): void {
  useAnnounceStore.getState().announce(text);
}
