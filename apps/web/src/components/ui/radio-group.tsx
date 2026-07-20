"use client";

import * as React from "react";
import { RadioGroup as RadioGroupPrimitive } from "radix-ui";
import { CircleIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET_PSEUDO } from "./focus-ring";

function RadioGroup({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Root>) {
  return (
    <RadioGroupPrimitive.Root
      data-slot="radio-group"
      // No `max-md:gap-*` override here, deliberately. RadioGroupItem's 44 px
      // touch area below md is reserved in its own margin (TOUCH_TARGET_PSEUDO),
      // so the items already push each other apart and this gap does not have to
      // compensate for them. Widening it here as well would double-count the
      // spacing and leave the group looking broken on a phone — and it would
      // only ever have fixed THIS one container, while every other place a
      // radio, checkbox or switch is stacked stayed overlapping.
      className={cn("grid gap-3", className)}
      {...props}
    />
  );
}

function RadioGroupItem({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-item"
      className={cn(
        "aspect-square size-4 shrink-0 rounded-full border border-input text-primary shadow-xs transition-[color,box-shadow]",
        // §8: see TOUCH_TARGET_PSEUDO — 16 px stays painted, 44 px is tappable.
        // The `relative` it adds is safe here: the dot below is positioned
        // against the Indicator wrapper (itself `relative`), not against this
        // element, so introducing a containing block here moves nothing.
        TOUCH_TARGET_PSEUDO,
        FOCUS_RING,
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40",
        className,
      )}
      {...props}
    >
      <RadioGroupPrimitive.Indicator
        data-slot="radio-group-indicator"
        className="relative flex items-center justify-center"
      >
        <CircleIcon
          aria-hidden
          className="absolute top-1/2 left-1/2 size-2 -translate-x-1/2 -translate-y-1/2 fill-primary"
        />
      </RadioGroupPrimitive.Indicator>
    </RadioGroupPrimitive.Item>
  );
}

export { RadioGroup, RadioGroupItem };
