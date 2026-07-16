import { Suspense } from "react";
import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/login-form";

export const metadata: Metadata = { title: "Sign in" };

/**
 * Local password login (Phase 8 identity, AUTH_MODE=local). The BFF holds the
 * session in httpOnly cookies; OIDC federation is a config swap in M7.
 */
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
          Use your TITAN credentials. Local development seeds{" "}
          <code className="font-mono text-xs">admin@titan.local</code>.
        </p>
        <Suspense>
          <LoginForm />
        </Suspense>
      </div>
    </main>
  );
}
