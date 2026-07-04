import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Sign in" };

// OIDC redirect flow lands in Phase 8 (AUTH_MODE=local) and hardens in M7.
export default function LoginPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-lg border bg-card p-8">
        <div className="flex items-center gap-2 font-semibold">
          <span
            aria-hidden
            className="inline-block size-2.5 rounded-full bg-primary"
          />
          TITAN
        </div>
        <h1 className="mt-6 text-lg font-semibold">Sign in</h1>
        <p className="mt-2 text-muted-foreground">
          Authentication arrives in Phase 8. Local development runs as the
          seeded admin until then.
        </p>
        <Button asChild className="mt-6 w-full">
          <Link href="/org">Continue to dashboard</Link>
        </Button>
      </div>
    </main>
  );
}
