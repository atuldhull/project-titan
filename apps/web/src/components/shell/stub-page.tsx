import type { ReactNode } from "react";

/**
 * Placeholder body for surfaces whose full build lands in a later phase.
 * Routes, titles, and sidebar highlighting are real now; only the content is
 * deferred (allowed for pages by the phase plan — never for backend code).
 */
export function StubPage({
  title,
  description,
  milestone = "M6 — Dashboard complete",
  children,
}: {
  title: string;
  description: string;
  milestone?: string;
  children?: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-xl font-semibold">{title}</h1>
      <div className="mt-4 rounded-lg border bg-card p-6">
        <p className="text-muted-foreground">{description}</p>
        <p className="mt-3 text-xs text-muted-foreground">
          Full surface arrives with{" "}
          <span className="font-medium">{milestone}</span>.
        </p>
        {children}
      </div>
    </div>
  );
}
