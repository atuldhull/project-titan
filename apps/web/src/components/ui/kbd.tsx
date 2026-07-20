import { cn } from "@/lib/utils";

/**
 * A key cap. Real `<kbd>`, so assistive tech announces it as keyboard input
 * rather than as decorative text (§9, TITAN-FRT-043).
 */
export function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded border bg-muted px-1.5 font-mono text-[10px] text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}
