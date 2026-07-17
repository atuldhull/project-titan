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
      <SheetContent className="w-full overflow-y-auto sm:max-w-md">
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
