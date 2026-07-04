import type { Metadata } from "next";

import { ThemeToggle } from "@/components/shell/theme-toggle";

export const metadata: Metadata = { title: "Appearance" };

// The canonical home of the theme choice (Vol 4 §3.2: toggled in
// Settings → Appearance, persisted, applied pre-paint).
export default function Page() {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-xl font-semibold">Appearance</h1>
      <div className="mt-4 flex items-center justify-between rounded-lg border bg-card p-6">
        <div>
          <p className="font-medium">Theme</p>
          <p className="mt-1 text-muted-foreground">
            Dark is the default; your choice persists and applies before first
            paint.
          </p>
        </div>
        <ThemeToggle className="size-10 border" />
      </div>
    </div>
  );
}
