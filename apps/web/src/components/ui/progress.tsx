"use client";

import * as React from "react";
import { Progress as ProgressPrimitive } from "radix-ui";

import { cn } from "@/lib/utils";

/**
 * Determinate progress bar (project progress, budget consumption). `indicator`
 * lets a caller swap the fill colour via a token class — e.g. the budget bar
 * turning warning/destructive as spend approaches the cap.
 */
function Progress({
  className,
  value,
  indicator,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & {
  indicator?: string;
}) {
  // `value={null}` is Radix's INDETERMINATE state — pass unknown progress
  // through as null rather than coercing to 0, or an unknown/failed board is
  // announced to a screen reader as "0 percent" and drawn as a real empty bar.
  const pct = value == null ? 0 : Math.min(100, Math.max(0, value));
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={value}
      className={cn(
        "relative h-1.5 w-full overflow-hidden rounded-full bg-muted",
        className,
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn(
          "h-full w-full flex-1 transition-all",
          indicator ?? "bg-primary",
        )}
        style={{ transform: `translateX(-${100 - pct}%)` }}
      />
    </ProgressPrimitive.Root>
  );
}

export { Progress };
