"use client";

import * as React from "react";
import { Tabs as TabsPrimitive } from "radix-ui";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET } from "./focus-ring";

function Tabs({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      className={cn("flex flex-col gap-4", className)}
      {...props}
    />
  );
}

function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn(
        // `max-w-full` is what makes the neighbouring `overflow-x-auto` do
        // anything at all. `w-fit` sizes the box to its CONTENT, so the
        // seven-tab project-detail list simply became 601px wide inside a
        // 343px parent and scrolled the PAGE — the overflow rule never
        // engaged, because there was no constrained width to overflow.
        // §8: wide content scrolls in its own container, never the body.
        "inline-flex w-fit max-w-full items-center justify-center gap-1 overflow-x-auto rounded-md bg-muted p-1",
        className,
      )}
      {...props}
    />
  );
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        // §8: `py-1` on 14 px text is a ~28 px trigger — the smallest touch
        // target in the app, and project detail puts SEVEN of them in a row.
        // Below md it becomes 44 px tall; at ≥ md the tab strip is unchanged.
        "inline-flex items-center justify-center gap-1.5 rounded-sm px-3 py-1 text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-xs",
        TOUCH_TARGET,
        FOCUS_RING,
        className,
      )}
      {...props}
    />
  );
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      {...props}
    />
  );
}

export { Tabs, TabsContent, TabsList, TabsTrigger };
