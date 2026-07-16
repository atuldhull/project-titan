import { cn } from "@/lib/utils";

/**
 * Loading placeholder. Per Vol 4 §6, skeletons are shaped like the content they
 * replace — never a spinner — so the layout does not jump when data lands.
 */
export function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}
