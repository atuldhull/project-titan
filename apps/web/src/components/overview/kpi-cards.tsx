"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * The §6.1 KPI stat cards. Each card is its own scope: it renders its own
 * loading/error state and degrades independently, so one failing panel never
 * blanks the row (§6 conventions).
 */
export function KpiCard({
  title,
  value,
  sub,
  href,
  state = "ready",
  onRetry,
}: {
  title: string;
  value?: string;
  sub?: React.ReactNode;
  href?: string;
  state?: "loading" | "error" | "ready";
  onRetry?: () => void;
}) {
  const body =
    state === "loading" ? (
      <>
        <Skeleton className="mt-1 h-7 w-16" />
        <Skeleton className="mt-2 h-3 w-24" />
      </>
    ) : state === "error" ? (
      <>
        <p className="mt-1 text-2xl font-semibold text-muted-foreground">—</p>
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 text-xs text-destructive underline underline-offset-4"
        >
          Failed to load · Retry
        </button>
      </>
    ) : (
      <>
        <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
        <div className="mt-1 text-xs text-muted-foreground">{sub}</div>
      </>
    );

  const card = (
    <div
      className={cn(
        "rounded-lg border bg-card p-4",
        href && "transition-colors hover:border-ring",
      )}
    >
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </p>
      {body}
    </div>
  );

  return href && state === "ready" ? (
    <Link href={href} className="block focus-visible:outline-none">
      {card}
    </Link>
  ) : (
    card
  );
}

/**
 * The Cost card. §6.1 wants "Cost today $4.12 · 61k tok [budget 68%]" — but the
 * backend exposes NO cost, spend or budget endpoint of any kind (verified: no
 * route, no field). Rather than invent a number or render a fake budget bar, the
 * card states plainly that the signal doesn't exist yet. It is deliberately not
 * an "error" state — nothing failed; the capability is absent.
 */
export function CostKpiCard() {
  return (
    <div className="rounded-lg border border-dashed bg-card p-4">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Cost today
      </p>
      <p className="mt-1 text-2xl font-semibold text-muted-foreground">—</p>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="mt-1 inline-flex cursor-help items-center gap-1 text-xs text-muted-foreground">
            <AlertTriangle aria-hidden className="size-3" />
            Not available yet
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-64">
          Spend and budget tracking has no backend endpoint yet, so this card
          has nothing truthful to show. It lights up when the cost API lands.
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
