"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { login } from "@/lib/bff/client";
import { useZodForm } from "@/lib/forms/use-zod-form";

const loginSchema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
  // Optional: only submitted once the server has asked for a second factor.
  totp_code: z.string().optional(),
});

/**
 * First-party login form (Phase 67). Posts to the BFF, which runs the
 * Authorization Code + PKCE flow and stores tokens in httpOnly cookies
 * (TITAN-SEC-010/013). When the account has MFA (or org policy requires it) the
 * BFF answers with an mfa signal and the form reveals a code field, then
 * re-submits — the password is still required on that second submit, so a
 * captured MFA prompt cannot be completed without it.
 */
export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [serverError, setServerError] = useState<string | null>(null);
  const [mfaStage, setMfaStage] = useState<"none" | "code" | "enroll">("none");
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useZodForm(loginSchema, {
    defaultValues: { email: "", password: "", totp_code: "" },
  });

  const onSubmit = handleSubmit(async ({ email, password, totp_code }) => {
    setServerError(null);
    const result = await login(email, password, totp_code || undefined);
    if (!result.ok) {
      if (result.mfa === "code") {
        setMfaStage("code");
        setServerError(null);
        return;
      }
      if (result.mfa === "enroll") {
        // Enrollment is a Settings flow, not something to complete on the login
        // form — say so plainly rather than showing an unusable code box.
        setMfaStage("enroll");
        setServerError(
          "This account must set up two-factor authentication before signing in.",
        );
        return;
      }
      setServerError(result.error ?? "Sign in failed");
      return;
    }
    const next = searchParams.get("next");
    router.replace(next && next.startsWith("/") ? next : "/org");
    router.refresh();
  });

  return (
    <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4" noValidate>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="email" className="text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          className="h-9 rounded-md border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
          aria-invalid={errors.email ? true : undefined}
          {...register("email")}
        />
        {errors.email ? (
          <p className="text-xs text-destructive">{errors.email.message}</p>
        ) : null}
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="password" className="text-sm font-medium">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          className="h-9 rounded-md border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
          aria-invalid={errors.password ? true : undefined}
          {...register("password")}
        />
        {errors.password ? (
          <p className="text-xs text-destructive">{errors.password.message}</p>
        ) : null}
      </div>

      {mfaStage === "code" ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="totp_code" className="text-sm font-medium">
            Authentication code
          </label>
          <input
            id="totp_code"
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            placeholder="123456"
            className="h-9 rounded-md border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
            {...register("totp_code")}
          />
          <p className="text-xs text-muted-foreground">
            Enter the code from your authenticator app, or a recovery code.
          </p>
        </div>
      ) : null}

      {serverError ? (
        <p role="alert" className="text-sm text-destructive">
          {serverError}
        </p>
      ) : null}

      <Button
        type="submit"
        className="w-full"
        disabled={isSubmitting || mfaStage === "enroll"}
      >
        {isSubmitting
          ? "Signing in…"
          : mfaStage === "code"
            ? "Verify"
            : "Sign in"}
      </Button>
    </form>
  );
}
