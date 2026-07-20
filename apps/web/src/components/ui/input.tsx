import * as React from "react";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET } from "./focus-ring";

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        // §8: 36 px is under the 44 px touch minimum, so it gains a min-height
        // below md. `h-9` is kept so the ≥ md box is byte-identical to before.
        "h-9 w-full min-w-0 rounded-md border bg-background px-3 py-1 text-sm shadow-xs transition-[color,box-shadow]",
        TOUCH_TARGET,
        "placeholder:text-muted-foreground",
        FOCUS_RING,
        "disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40",
        className,
      )}
      {...props}
    />
  );
}

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        // No TOUCH_TARGET here: `min-h-16` (64 px) already clears 44 px.
        "field-sizing-content min-h-16 w-full rounded-md border bg-background px-3 py-2 text-sm shadow-xs transition-[color,box-shadow]",
        "placeholder:text-muted-foreground",
        FOCUS_RING,
        "disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40",
        className,
      )}
      {...props}
    />
  );
}

export { Input, Textarea };
