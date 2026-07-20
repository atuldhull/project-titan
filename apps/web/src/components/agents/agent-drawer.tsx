"use client";

/**
 * The agent detail as a slide-over (§6.5's "agent detail drawer").
 *
 * A Sheet driven by a ROUTE rather than by local state: closing it is
 * `router.back()`, which unwinds the navigation that opened it. That keeps the
 * URL, the back button and the drawer in agreement — the thing a search-param
 * overlay cannot do.
 *
 * `open` is hard-coded true because this component only exists while the
 * interceptor is matched; the route IS the open state.
 *
 * Verified for §9 rather than re-implemented: Radix's Dialog (which Sheet is)
 * already gives this a focus trap, `aria-modal`, focus restored to the opener on
 * close, and Escape → `onOpenChange(false)` → `router.back()`. TITAN-FRT-042's
 * "no traps" and §10's Escape row are therefore satisfied by the primitive, and
 * the global `Escape` binding deliberately does not fight it — `global-shortcuts`
 * only closes the palette and the help overlay, leaving Radix's own dialogs to
 * Radix.
 */

import { useRouter } from "next/navigation";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { AgentDetailScreen } from "@/components/agents/agent-detail-screen";
import { cn } from "@/lib/utils";

export function AgentDrawer({ agentId }: { agentId: string }) {
  const router = useRouter();
  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) {
          router.back();
        }
      }}
    >
      {/*
       * §8: "768–1023 … detail panels → bottom Sheets". `side` is a prop, and
       * changing it per breakpoint would need `matchMedia` — which this app does
       * not have and which has no jsdom mock — so the GEOMETRY is overridden in
       * CSS instead: a bottom sheet under lg, the wireframed right-hand
       * slide-over at lg+. `max-md:` is not enough; the middle band wants it too.
       *
       * Every `side="right"` class has to be answered or tailwind-merge leaves
       * the old one winning: `inset-y-0`+`right-0`+`h-full`+`w-3/4`+`border-l`,
       * plus the `sm:max-w-sm` that would otherwise cap this at 384px.
       *
       * That last one has to be answered AT ITS OWN BREAKPOINT. tailwind-merge
       * only ever de-duplicates within one modifier prefix, so a bare
       * `max-w-none` does not touch `sm:max-w-sm` — it survives, and from 640px
       * up the "bottom sheet" becomes a 384px-wide slab pinned to the bottom
       * LEFT corner (`inset-x-0` + a max-width is over-constrained, so `right`
       * is the one that gets dropped). 640–1023 is the band §8 asks for a bottom
       * sheet in, so the bug lands squarely on the case this was written for.
       * Hence `sm:max-w-none`, and `lg:max-w-md` to restore the cap for the
       * slide-over.
       *
       * `85dvh`, not `85vh`: `vh` is the LARGE viewport on mobile, so the sheet
       * would extend under the browser's own chrome and lose its last rows.
       */}
      <SheetContent
        className={cn(
          "overflow-y-auto",
          "inset-x-0 inset-y-auto bottom-0 h-[85dvh] w-full max-w-none rounded-t-lg border-t border-l-0 sm:max-w-none",
          "lg:inset-x-auto lg:inset-y-0 lg:right-0 lg:h-full lg:max-w-md lg:rounded-t-none lg:border-t-0 lg:border-l",
        )}
      >
        <SheetHeader className="sr-only">
          {/* Radix requires a title for the dialog's accessible name; the
              visible heading lives in AgentDetail, so this is the a11y one. */}
          <SheetTitle>Agent detail</SheetTitle>
          <SheetDescription>
            State, current work, runs and messages for this agent.
          </SheetDescription>
        </SheetHeader>
        <div className="px-4 pb-6">
          <AgentDetailScreen agentId={agentId} />
        </div>
      </SheetContent>
    </Sheet>
  );
}
