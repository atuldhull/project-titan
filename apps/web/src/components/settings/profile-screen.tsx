"use client";

import { useEffect } from "react";
import { toast } from "sonner";
import { z } from "zod";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { TypedConfirmDialog } from "@/components/settings/typed-confirm-dialog";
import { useZodForm } from "@/lib/forms/use-zod-form";
import {
  useProfile,
  useRevokeSession,
  useSessions,
  useUpdateProfile,
} from "@/lib/settings/hooks";
import { errorMessage } from "@/lib/settings/errors";
import { useState } from "react";

/**
 * Settings → Profile (Vol 4 §6.13).
 *
 * §6.13 lists "name, email, avatar, session list with revoke". Three of those
 * are real. **There is no avatar** — no column on `users`, no blob store, no
 * upload route anywhere in TITAN — so this renders initials and says so rather
 * than showing an upload control that could not persist anything.
 *
 * **Email is read-only.** It is the local-auth login identity and the table's
 * unique key; changing it is an account-recovery flow with a verification step,
 * and none exists. The API rejects the field outright, so an editable input
 * would only produce a 422.
 */
const schema = z.object({
  display_name: z
    .string()
    .min(1, "Display name is required.")
    .max(200, "Must be at most 200 characters."),
});

function initials(name: string, email: string): string {
  const source = name.trim() || email;
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return (parts[0]?.[0] ?? "?").concat(parts[1]?.[0] ?? "").toUpperCase();
}

export function ProfileScreen() {
  const profile = useProfile();
  const update = useUpdateProfile();
  const form = useZodForm(schema, { defaultValues: { display_name: "" } });
  const { reset } = form;

  // Seed the form once the server value lands. `reset` (not setValue) so the
  // form's dirty baseline becomes the SAVED value — otherwise Save is enabled
  // on first paint with nothing changed.
  useEffect(() => {
    if (profile.data) {
      reset({ display_name: profile.data.display_name });
    }
  }, [profile.data, reset]);

  if (profile.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (profile.isError || !profile.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load your profile. {errorMessage(profile.error)}{" "}
        <button className="underline" onClick={() => void profile.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  const me = profile.data;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
          <CardDescription>Your account details.</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit(async (values) => {
              try {
                await update.mutateAsync(values.display_name);
                reset(values); // new clean baseline — Save disables again
                toast.success("Profile updated");
              } catch (error) {
                toast.error(errorMessage(error));
              }
            })}
          >
            <div className="flex items-center gap-3">
              <span
                aria-hidden
                className="flex size-12 items-center justify-center rounded-full bg-secondary text-sm font-medium text-secondary-foreground"
              >
                {initials(me.display_name, me.email)}
              </span>
              <p className="text-xs text-muted-foreground">
                TITAN stores no avatar image — initials are derived from your
                name.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="display_name">Display name</Label>
              <Input
                id="display_name"
                aria-invalid={Boolean(form.formState.errors.display_name)}
                {...form.register("display_name")}
              />
              {form.formState.errors.display_name ? (
                <p className="text-xs text-destructive" role="alert">
                  {form.formState.errors.display_name.message}
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input id="email" value={me.email} readOnly disabled />
              <p className="text-xs text-muted-foreground">
                Your sign-in identity. Changing it needs a verification flow
                that TITAN does not have yet.
              </p>
            </div>

            <div className="space-y-2">
              <Label>Roles</Label>
              <div className="flex flex-wrap gap-1">
                {(me.roles ?? []).length === 0 ? (
                  <span className="text-xs text-muted-foreground">
                    No org-wide roles.
                  </span>
                ) : (
                  (me.roles ?? []).map((role) => (
                    <Badge key={role} variant="secondary">
                      {role}
                    </Badge>
                  ))
                )}
              </div>
            </div>

            <Button
              type="submit"
              // §6.13: "Save is disabled until dirty".
              disabled={!form.formState.isDirty || update.isPending}
            >
              {update.isPending ? "Saving…" : "Save"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <SessionsCard />
    </div>
  );
}

function SessionsCard() {
  const sessions = useSessions();
  const revoke = useRevokeSession();
  const [target, setTarget] = useState<string | null>(null);

  const rows = sessions.data ?? [];
  const active = rows.filter((s) => !s.revoked);
  const chosen = rows.find((s) => s.family_id === target);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Signed-in devices</CardTitle>
        <CardDescription>
          One entry per sign-in. Refreshing a session does not create a new one.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {sessions.isLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : sessions.isError ? (
          <p className="text-sm text-muted-foreground" role="status">
            Couldn’t load sessions. {errorMessage(sessions.error)}
          </p>
        ) : active.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No stored sessions. Signing in with a password creates one; a
            short-lived token on its own does not.
          </p>
        ) : (
          <ul className="divide-y">
            {active.map((session) => (
              <li
                key={session.family_id}
                className="flex flex-wrap items-center justify-between gap-2 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs">
                    {session.family_id.slice(0, 8)}
                    {session.current ? (
                      <Badge variant="secondary" className="ml-2">
                        this device
                      </Badge>
                    ) : null}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Started {new Date(session.created_at).toLocaleString()} ·
                    last used {new Date(session.last_used_at).toLocaleString()}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setTarget(session.family_id)}
                >
                  Revoke
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>

      <TypedConfirmDialog
        open={target !== null}
        title="Revoke this session?"
        phrase="revoke"
        pending={revoke.isPending}
        description={
          <>
            <p>
              The device signing in with it will be signed out on its next
              request.
            </p>
            {chosen?.current ? (
              <p className="font-medium text-destructive">
                This is the session you are using — you will be signed out.
              </p>
            ) : null}
          </>
        }
        confirmLabel="Revoke session"
        onCancel={() => setTarget(null)}
        onConfirm={async () => {
          if (!target) return;
          try {
            await revoke.mutateAsync(target);
            toast.success("Session revoked");
          } catch (error) {
            toast.error(errorMessage(error));
          } finally {
            setTarget(null);
          }
        }}
      />
    </Card>
  );
}
