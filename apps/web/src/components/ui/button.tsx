import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET, TOUCH_TARGET_ICON } from "./focus-ring";

const buttonVariants = cva(
  `inline-flex shrink-0 items-center justify-center gap-2 rounded-md text-sm font-medium whitespace-nowrap transition-all ${FOCUS_RING} disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4`,
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary/90",
        destructive:
          "bg-destructive text-white hover:bg-destructive/90 dark:bg-destructive/60",
        outline:
          "border bg-background shadow-xs hover:bg-accent hover:text-accent-foreground dark:border-input dark:bg-input/30 dark:hover:bg-input/50",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-secondary/80",
        ghost:
          "hover:bg-accent hover:text-accent-foreground dark:hover:bg-accent/50",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: `h-9 px-4 py-2 has-[>svg]:px-3 ${TOUCH_TARGET}`,
        xs: `h-6 gap-1 rounded-md px-2 text-xs has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3 ${TOUCH_TARGET}`,
        sm: `h-8 gap-1.5 rounded-md px-3 has-[>svg]:px-2.5 ${TOUCH_TARGET}`,
        lg: `h-10 rounded-md px-6 has-[>svg]:px-4 ${TOUCH_TARGET}`,
        icon: `size-9 ${TOUCH_TARGET_ICON}`,
        "icon-xs": `size-6 rounded-md [&_svg:not([class*='size-'])]:size-3 ${TOUCH_TARGET_ICON}`,
        "icon-sm": `size-8 ${TOUCH_TARGET_ICON}`,
        "icon-lg": `size-10 ${TOUCH_TARGET_ICON}`,
      },
    },
    compoundVariants: [
      {
        // The `link` variant is a text link wearing button clothes; it sits
        // inline in sentences and inside table cells. Growing it to 44 px would
        // blow out the line box around it, and WCAG 2.5.5/2.5.8 exempt targets
        // in a block of text for exactly that reason. cva emits compound
        // variants AFTER the size classes and `cn` runs tailwind-merge over the
        // result, so this genuinely cancels the `max-md:min-h-11` above rather
        // than fighting it on specificity.
        variant: "link",
        className: "max-md:min-h-0 max-md:min-w-0",
      },
    ],
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "button";

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
