"use client";

import { Moon, Sun } from "lucide-react";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET_ICON } from "@/components/ui/focus-ring";

/*
 * Light/dark toggle (TITAN-FRT-011, ADR-FRT-06). Persists to localStorage;
 * the root layout's blocking script re-applies the choice before first paint.
 * Icons swap via CSS dark: variants, so SSR markup never depends on the theme.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const toggle = () => {
    const root = document.documentElement;
    const toLight = root.classList.contains("dark");
    root.classList.toggle("dark", !toLight);
    localStorage.setItem("titan.theme", toLight ? "light" : "dark");
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label="Toggle theme"
      className={cn(
        // `shrink-0`: the top bar is a crowded flex row at 375px, and without
        // it this measured 29px WIDE — under even its own size-8, let alone
        // §8's 44px minimum. It is a raw <button>, not the Button primitive,
        // so it inherits neither the touch minimum nor the §9 focus ring and
        // has to opt into both explicitly.
        "inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground",
        "hover:bg-accent hover:text-foreground",
        TOUCH_TARGET_ICON,
        FOCUS_RING,
        className,
      )}
    >
      <Sun aria-hidden className="hidden size-4 dark:block" />
      <Moon aria-hidden className="size-4 dark:hidden" />
    </button>
  );
}
