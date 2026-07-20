"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ResponsiveTable,
  type ResponsiveColumn,
} from "@/components/settings/responsive-table";
import { errorMessage } from "@/lib/settings/errors";
import {
  useNotificationPreferences,
  useSaveNotificationPreferences,
} from "@/lib/settings/hooks";
import type { NotificationPreferences } from "@titan/api-client";

/** category → the channels checked for it. */
type Matrix = Record<string, string[]>;

function matrixOf(data: NotificationPreferences): Matrix {
  const out: Matrix = {};
  for (const row of data.categories ?? []) {
    out[row.category] = [...(row.channels ?? [])];
  }
  return out;
}

/** Order-insensitive comparison key, so re-checking a box you just unchecked
 *  makes the form clean again rather than merely "different". */
function signature(matrix: Matrix): string {
  return Object.keys(matrix)
    .sort()
    .map((category) => `${category}=${[...matrix[category]].sort().join(",")}`)
    .join("|");
}

/**
 * Settings → Notifications (Vol 4 §6.13, §7.1 matrix).
 *
 * The rows and columns both come from the server. Categories are §7.1's event
 * classes with their severity, and `available_channels` is what actually has a
 * delivery path today — hard-coding either would drift the moment a channel is
 * implemented.
 *
 * A category flagged `is_default` has NO stored row: it is tracking the spec
 * default rather than being pinned to a choice. Checking anything in that row
 * pins it, which is a real change even when the boxes look identical.
 */
export function NotificationsScreen() {
  const prefs = useNotificationPreferences();
  const save = useSaveNotificationPreferences();
  const [draft, setDraft] = useState<Matrix | null>(null);

  const baseline = useMemo(
    () => (prefs.data ? matrixOf(prefs.data) : null),
    [prefs.data],
  );

  // Seed from the server, and re-seed after a save — the mutation writes the
  // response into the cache, so `baseline` becomes the newly saved matrix and
  // the form goes clean without a refetch.
  useEffect(() => {
    if (baseline) {
      setDraft(baseline);
    }
  }, [baseline]);

  if (prefs.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (prefs.isError || !prefs.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load notification preferences. {errorMessage(prefs.error)}{" "}
        <button className="underline" onClick={() => void prefs.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  const data = prefs.data;
  const categories = data.categories ?? [];
  const channels = data.available_channels ?? [];
  const unavailable = data.unavailable_channels ?? [];
  const current = draft ?? baseline ?? {};
  const dirty = baseline !== null && signature(current) !== signature(baseline);

  const toggle = (category: string, channel: string, on: boolean) => {
    setDraft((previous) => {
      const source = previous ?? baseline ?? {};
      const selected = source[category] ?? [];
      const next = on
        ? selected.includes(channel)
          ? selected
          : [...selected, channel]
        : selected.filter((name) => name !== channel);
      return { ...source, [category]: next };
    });
  };

  /**
   * The columns are SERVER-DRIVEN: `available_channels` is what actually has a
   * delivery path today, so the card layout has to be generated from the same
   * list the table header is (§8's "tables → card lists" over a matrix). Both
   * layouts consume this one array, so a channel that appears on the deployment
   * appears in both or in neither — never in the table alone.
   *
   * A matrix is also the case where the card layout is not merely a nicety: at
   * 375px a row of N centred checkboxes has no visible header to say WHICH
   * channel each one is, whereas the card renders `slack ▸ ☑` with the label
   * attached.
   */
  const columns: readonly ResponsiveColumn<(typeof categories)[number]>[] = [
    {
      key: "event",
      header: "Event",
      primary: true,
      cell: (category) => (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{category.category}</span>
          <Badge variant="outline">{category.severity}</Badge>
          {category.is_default ? (
            <span
              className="text-xs font-normal text-muted-foreground"
              title="No saved choice — this row follows the shipped default."
            >
              default
            </span>
          ) : null}
        </div>
      ),
    },
    ...channels.map(
      (channel): ResponsiveColumn<(typeof categories)[number]> => ({
        key: channel,
        header: channel,
        align: "center",
        cell: (category) => (
          <Checkbox
            aria-label={`${channel} for ${category.category}`}
            checked={(current[category.category] ?? []).includes(channel)}
            disabled={save.isPending}
            onCheckedChange={(next) =>
              toggle(category.category, channel, next === true)
            }
          />
        ),
      }),
    ),
  ];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Notifications</CardTitle>
          {/* Honest about the gap: the matrix PERSISTS but nothing reads it
              back yet. The in-app delivery paths (toast, bell, attention
              queue) were built in Phase 57 against §7.1's defaults and do not
              consult these rows, so a saved preference is durable and inert.
              "Unchecking silences it" would be a straight lie today. */}
          <CardDescription>
            Which channels carry each kind of event. Your choices are saved, but
            not yet honoured at delivery — the in-app surfaces still follow the
            §7.1 defaults until a later phase reads this matrix.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {categories.length === 0 || channels.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No notification categories are configured on this deployment, so
              there is nothing to route.
            </p>
          ) : (
            <>
              <ResponsiveTable
                rows={categories}
                rowKey={(category) => category.category}
                columns={columns}
                caption="Which channels carry each event category"
              />

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  disabled={!dirty || save.isPending}
                  onClick={async () => {
                    try {
                      // The PUT REPLACES the whole matrix. Sending only the
                      // rows that changed would make "unchecked" and
                      // "unmentioned" indistinguishable — and clearing every
                      // channel for a category is something a user is entitled
                      // to mean — so every category goes on every save.
                      await save.mutateAsync(
                        categories.map((category) => ({
                          category: category.category,
                          channels: current[category.category] ?? [],
                        })),
                      );
                      toast.success("Notification preferences saved");
                    } catch (error) {
                      toast.error(errorMessage(error));
                    }
                  }}
                >
                  {save.isPending ? "Saving…" : "Save changes"}
                </Button>
                <Button
                  variant="ghost"
                  disabled={!dirty || save.isPending}
                  onClick={() => setDraft(baseline)}
                >
                  Reset
                </Button>
              </div>
            </>
          )}

          {unavailable.length > 0 ? (
            <p className="text-xs text-muted-foreground">
              Not offered: {unavailable.join(", ")}. The category exists in the
              §7.1 spec but has no delivery implementation behind it yet, so a
              checkbox here would route events nowhere.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
