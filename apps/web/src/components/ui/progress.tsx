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
  label,
  "aria-label": ariaLabel,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & {
  indicator?: string;
  /**
   * What this bar measures. REQUIRED in practice: Radix renders
   * `role="progressbar"`, and a progressbar with no accessible name is an axe
   * `aria-progressbar-name` violation at SERIOUS impact — which is what the
   * §9 gate blocks on. A screen reader otherwise announces a bare "62 percent"
   * with nothing to say 62 percent of what.
   */
  label?: string;
}) {
  // `value={null}` is Radix's INDETERMINATE state — pass unknown progress
  // through as null rather than coercing to 0, or an unknown/failed board is
  // announced to a screen reader as "0 percent".
  //
  // TITAN-FRT-044 (status is never colour-only, and never AMBIGUOUS): passing
  // null through to Radix fixed the ANNOUNCEMENT but not the pixels. Both
  // states still translated the fill fully off-screen, so "we don't know this
  // project's progress" and "this project is at 0 %" drew the identical empty
  // grey bar — the reader gets the distinction, the sighted user does not.
  // Indeterminate now renders a striped track instead of a fill, so the two are
  // distinguishable at a glance and in a grayscale audit.
  const indeterminate = value == null;
  const pct = indeterminate ? 0 : Math.min(100, Math.max(0, value));
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      data-state-indeterminate={indeterminate ? "" : undefined}
      // An explicit `aria-label` from the call site wins; `label` is the
      // ergonomic spelling. The fallback is deliberately generic rather than
      // absent — an unnamed progressbar fails the gate outright.
      aria-label={ariaLabel ?? label ?? "Progress"}
      value={value}
      className={cn(
        "relative h-1.5 w-full overflow-hidden rounded-full bg-muted",
        className,
      )}
      {...props}
    >
      {indeterminate ? (
        // A static repeating-gradient hatch, NOT an animated barber pole:
        // TITAN-FRT-045 kills animation under prefers-reduced-motion, and a
        // meaning that only exists while something is moving disappears with
        // it. `--color-border` is a theme token, so this satisfies the
        // no-raw-colour rule (TITAN-FRT-010) in both themes.
        <div
          data-slot="progress-indeterminate"
          aria-hidden
          className="h-full w-full"
          style={{
            backgroundImage:
              "repeating-linear-gradient(135deg, var(--color-border) 0 4px, transparent 4px 8px)",
          }}
        />
      ) : (
        <ProgressPrimitive.Indicator
          data-slot="progress-indicator"
          className={cn(
            "h-full w-full flex-1 transition-all",
            indicator ?? "bg-primary",
          )}
          style={{ transform: `translateX(-${100 - pct}%)` }}
        />
      )}
    </ProgressPrimitive.Root>
  );
}

export { Progress };
