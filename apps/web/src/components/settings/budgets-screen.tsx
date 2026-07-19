"use client";

import { useEffect } from "react";
import { toast } from "sonner";
import { z } from "zod";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCan } from "@/lib/auth";
import { useZodForm } from "@/lib/forms/use-zod-form";
import { errorMessage } from "@/lib/settings/errors";
import {
  useLlmSettings,
  useUpdateLlmSettings,
  type LlmSettingsPatch,
} from "@/lib/settings/hooks";

/**
 * Settings → Budgets (Vol 4 §6.13, §8.4 caps).
 *
 * **`per_project_usd` is not per-project.** There is no per-project override
 * table anywhere in TITAN — the gateway reads this one number out of the merged
 * config and applies it to whatever project id the call happens to carry. Two
 * projects do not get two ceilings; they get the same ceiling counted
 * separately. The UI says so in words as well as in this comment, because the
 * field name alone would mislead an operator into thinking a busy project can
 * be capped without touching a quiet one.
 *
 * **A cap is a stop, not a warning.** On a breach the gateway persists a hold
 * and answers 402, and the hold keeps blocking calls until it is cleared —
 * `fallback_then_pause` only differs by trying a local (Ollama) candidate
 * first, and pauses too when there isn't one. Any other stored value behaves
 * as `pause`, so those are the only two the picker offers.
 */
const optionalUsd = z.preprocess(
  // An emptied input arrives as "" and `Number("")` is 0 — which would read as
  // "cap everything at zero dollars", the opposite of "no cap".
  (value) =>
    typeof value === "string" && value.trim() === "" ? undefined : value,
  z.coerce
    .number({ invalid_type_error: "Enter an amount in dollars." })
    .nonnegative("Must be 0 or more.")
    .optional(),
);

const schema = z.object({
  per_project_usd: optionalUsd,
  per_task_usd: optionalUsd,
  action_on_exceeded: z.enum(["pause", "fallback_then_pause"]),
});

const ACTION_LABELS: Record<string, string> = {
  pause: "Pause — hold the budget and fail the call (402)",
  fallback_then_pause: "Try a local model first, then pause",
};

export function BudgetsScreen() {
  const settings = useLlmSettings();
  const update = useUpdateLlmSettings();
  const canWrite = useCan("settings:write");
  const form = useZodForm(schema, {
    defaultValues: {
      per_project_usd: undefined,
      per_task_usd: undefined,
      action_on_exceeded: "pause",
    },
  });
  const { reset } = form;

  const budgets = settings.data?.budgets;
  const storedAction = budgets?.action_on_exceeded ?? null;

  // Seed once the server value lands. `reset` (not setValue) so the dirty
  // baseline is the SAVED value — otherwise Save is live on first paint.
  //
  // The caps arrive as STRINGS: they are `Numeric` on the wire so the totals
  // survive to the micro-dollar. Parse only here, at the edge of the form.
  useEffect(() => {
    if (!settings.data) return;
    reset({
      per_project_usd:
        budgets?.per_project_usd != null
          ? Number(budgets.per_project_usd)
          : undefined,
      per_task_usd:
        budgets?.per_task_usd != null
          ? Number(budgets.per_task_usd)
          : undefined,
      action_on_exceeded:
        storedAction === "fallback_then_pause"
          ? "fallback_then_pause"
          : "pause",
    });
  }, [
    settings.data,
    budgets?.per_project_usd,
    budgets?.per_task_usd,
    storedAction,
    reset,
  ]);

  if (settings.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (settings.isError || !settings.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load budget settings. {errorMessage(settings.error)}{" "}
        <button className="underline" onClick={() => void settings.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  const action = form.watch("action_on_exceeded");
  const uncapped =
    budgets?.per_project_usd == null && budgets?.per_task_usd == null;
  const unknownAction =
    storedAction !== null &&
    storedAction !== "pause" &&
    storedAction !== "fallback_then_pause";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Budgets</CardTitle>
        <CardDescription>
          Spend ceilings for LLM calls (§8.4). A cap is checked before the call
          is dispatched, against the estimated cost.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={form.handleSubmit(async (values) => {
            try {
              const patch: LlmSettingsPatch = {
                ...(values.per_project_usd !== undefined
                  ? { per_project_usd: values.per_project_usd }
                  : {}),
                ...(values.per_task_usd !== undefined
                  ? { per_task_usd: values.per_task_usd }
                  : {}),
                action_on_exceeded: values.action_on_exceeded,
              };
              await update.mutateAsync(patch);
              reset(values); // new clean baseline — Save disables again
              toast.success("Budgets saved — the next call uses them");
            } catch (error) {
              toast.error(errorMessage(error));
            }
          })}
        >
          {uncapped ? (
            <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
              No cap is set, so LLM spend is unlimited. Enter an amount to
              enforce one.
            </p>
          ) : null}

          <div className="space-y-2">
            <Label htmlFor="per_project_usd">Project cap (USD)</Label>
            <Input
              id="per_project_usd"
              inputMode="decimal"
              className="max-w-xs"
              placeholder="no cap"
              disabled={!canWrite}
              aria-invalid={Boolean(form.formState.errors.per_project_usd)}
              {...form.register("per_project_usd")}
            />
            {form.formState.errors.per_project_usd ? (
              <p className="text-xs text-destructive" role="alert">
                {form.formState.errors.per_project_usd.message}
              </p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              One ceiling, applied to every project. TITAN has no per-project
              override, so this is the same number for all of them — it is not a
              way to cap one project and leave another alone.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="per_task_usd">Task cap (USD)</Label>
            <Input
              id="per_task_usd"
              inputMode="decimal"
              className="max-w-xs"
              placeholder="no cap"
              disabled={!canWrite}
              aria-invalid={Boolean(form.formState.errors.per_task_usd)}
              {...form.register("per_task_usd")}
            />
            {form.formState.errors.per_task_usd ? (
              <p className="text-xs text-destructive" role="alert">
                {form.formState.errors.per_task_usd.message}
              </p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Everything one task spends across all of its agent turns.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="action_on_exceeded">When a cap is reached</Label>
            <Select
              value={action}
              disabled={!canWrite}
              onValueChange={(value) =>
                form.setValue(
                  "action_on_exceeded",
                  value as "pause" | "fallback_then_pause",
                  { shouldDirty: true },
                )
              }
            >
              <SelectTrigger id="action_on_exceeded" className="max-w-md">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(ACTION_LABELS).map(([value, label]) => (
                  <SelectItem key={value} value={value}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Either way the budget goes on hold and further calls are refused
              until it is cleared. There is no “warn and keep spending”.
            </p>
            {unknownAction ? (
              <p className="text-xs text-muted-foreground">
                The stored value is{" "}
                <span className="font-mono">{storedAction}</span>, which the
                gateway treats as pause. Saving will replace it.
              </p>
            ) : null}
          </div>

          <Button
            type="submit"
            disabled={!canWrite || !form.formState.isDirty || update.isPending}
          >
            {update.isPending ? "Saving…" : "Save"}
          </Button>

          {!canWrite ? (
            <p className="text-xs text-muted-foreground">
              Read-only — changing budgets needs the owner or admin role.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Clearing a field leaves the existing cap in place: the API applies
              only the fields it is sent. Remove a cap by editing{" "}
              <code className="font-mono">config/models.overrides.yaml</code>.
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  );
}
