"use client";

import { Moon, Sun } from "lucide-react";

import { cn } from "@/lib/utils";

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
        "inline-flex size-8 items-center justify-center rounded-md text-muted-foreground",
        "hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      <Sun aria-hidden className="hidden size-4 dark:block" />
      <Moon aria-hidden className="size-4 dark:hidden" />
    </button>
  );
}
