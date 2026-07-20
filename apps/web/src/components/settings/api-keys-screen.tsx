"use client";

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { z } from "zod";

import { TypedConfirmDialog } from "@/components/settings/typed-confirm-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ResponsiveTable,
  type ResponsiveColumn,
} from "@/components/settings/responsive-table";
import { useZodForm } from "@/lib/forms/use-zod-form";
import { errorMessage } from "@/lib/settings/errors";
import {
  useApiKeys,
  useCreateApiKey,
  useRevokeApiKey,
} from "@/lib/settings/hooks";
import type { ApiKeyOut } from "@titan/api-client";

/**
 * Settings → API Keys (Vol 4 §6.13).
 *
 * **The secret exists for exactly one render.** `POST /auth/api-keys` is the
 * only response in TITAN that ever carries `api_key`; the server stores a hash
 * and every later read returns just the `prefix`. There is no "show again", no
 * re-issue, and no support path that can recover it — losing it means creating
 * a new key. That is why creation opens a modal the operator has to dismiss
 * deliberately rather than dropping the value into a toast that auto-expires.
 *
 * Revoked keys stay in the table. They are the audit trail for "what was this
 * prefix, and when did it stop working" — deleting the row would erase the only
 * record that a key ever existed.
 */
const schema = z.object({
  name: z
    .string()
    .min(1, "A name is required.")
    .max(200, "Must be at most 200 characters."),
});

export function ApiKeysScreen() {
  const keys = useApiKeys();
  const create = useCreateApiKey();
  const revoke = useRevokeApiKey();
  const form = useZodForm(schema, { defaultValues: { name: "" } });
  const { reset } = form;

  // The one-time secret, held only in this component's state. Nothing writes it
  // to storage or to the query cache: a cached secret would survive navigation
  // and outlive the moment the user is supposed to copy it.
  const [issued, setIssued] = useState<{ name: string; secret: string } | null>(
    null,
  );
  const [target, setTarget] = useState<ApiKeyOut | null>(null);

  // Active first, newest first within each group. Sorting by `revoked_at`
  // presence rather than by date keeps a key revoked minutes ago from sitting
  // above keys still in use.
  const rows = useMemo(() => {
    const source = keys.data ?? [];
    return [...source].sort((a, b) => {
      const revokedDiff =
        Number(Boolean(a.revoked_at)) - Number(Boolean(b.revoked_at));
      if (revokedDiff !== 0) {
        return revokedDiff;
      }
      return Date.parse(b.created_at) - Date.parse(a.created_at);
    });
  }, [keys.data]);

  // Declared once; `ResponsiveTable` renders it as a table at md+ and as cards
  // below (§8). `name` is the card title because it is the only thing an
  // operator has to recognise a key by — the prefix is the machine's handle,
  // not the human's.
  const columns: readonly ResponsiveColumn<ApiKeyOut>[] = [
    {
      key: "name",
      header: "Name",
      primary: true,
      cell: (key) => <span className="font-medium">{key.name}</span>,
    },
    {
      key: "prefix",
      header: "Prefix",
      cell: (key) => <span className="font-mono text-xs">{key.prefix}</span>,
    },
    {
      key: "created",
      header: "Created",
      cell: (key) => (
        <span className="text-xs text-muted-foreground">
          {new Date(key.created_at).toLocaleString()}
        </span>
      ),
    },
    {
      key: "last-used",
      header: "Last used",
      cell: (key) => (
        <span className="text-xs text-muted-foreground">
          {key.last_used_at ? (
            new Date(key.last_used_at).toLocaleString()
          ) : (
            <span className="italic">never</span>
          )}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (key) =>
        key.revoked_at ? (
          <Badge variant="outline">Revoked</Badge>
        ) : (
          <Badge variant="secondary">Active</Badge>
        ),
    },
    {
      key: "actions",
      header: "Actions",
      align: "right",
      cell: (key) =>
        key.revoked_at ? (
          <span className="text-xs text-muted-foreground">—</span>
        ) : (
          // `min-h-11` under md only: §8's 44px touch minimum. The default
          // `size="sm"` button is 32px, which is a miss on the one control in
          // this screen that destroys something.
          <Button
            size="sm"
            variant="outline"
            className="min-h-11 md:min-h-0"
            onClick={() => setTarget(key)}
          >
            Revoke
          </Button>
        ),
    },
  ];

  if (keys.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-56 w-full" />
      </div>
    );
  }

  if (keys.isError || !keys.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load your API keys. {errorMessage(keys.error)}{" "}
        <button className="underline" onClick={() => void keys.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Create a key</CardTitle>
          {/* NOT "carries your own roles". `exchange_api_key` mints an AGENT
              principal with the fixed `operator` bundle — never the roles of
              the human who created it — and `operator` is project-scoped, so a
              key with no agent binding reaches very little. Claiming otherwise
              would invite an owner to mint one expecting owner powers. */}
          <CardDescription>
            A key authenticates as a machine principal holding the{" "}
            <span className="font-mono">operator</span> role — not as you, and
            not with your roles. Name it after where it will be used; that name
            is all you will have to recognise it by later.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit(async (values) => {
              try {
                const result = await create.mutateAsync(values.name);
                // Capture the secret BEFORE clearing the form — `result.api_key`
                // is the only copy that will ever exist.
                setIssued({ name: values.name, secret: result.api_key });
                reset({ name: "" });
              } catch (error) {
                toast.error(errorMessage(error));
              }
            })}
          >
            <div className="space-y-2">
              <Label htmlFor="api-key-name">Name</Label>
              <Input
                id="api-key-name"
                className="max-w-md"
                placeholder="ci-deploy"
                aria-invalid={Boolean(form.formState.errors.name)}
                {...form.register("name")}
              />
              {form.formState.errors.name ? (
                <p className="text-xs text-destructive" role="alert">
                  {form.formState.errors.name.message}
                </p>
              ) : null}
            </div>
            <Button
              type="submit"
              disabled={!form.formState.isDirty || create.isPending}
            >
              {create.isPending ? "Creating…" : "Create key"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Your keys</CardTitle>
          <CardDescription>
            Revoked keys are kept as a record of what the prefix belonged to.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No API keys yet. Create one above to call the TITAN API without a
              browser session.
            </p>
          ) : (
            <ResponsiveTable
              rows={rows}
              rowKey={(key) => key.id}
              columns={columns}
              caption="Your API keys, active first"
              empty="No API keys yet. Create one above to call the TITAN API without a browser session."
              // Applies to the card as well as the row: a revoked key must read
              // as revoked in BOTH layouts, and the Status badge alone is one
              // signal where this is two (TITAN-FRT-044 in the small).
              rowClassName={(key) =>
                key.revoked_at ? "opacity-60" : undefined
              }
            />
          )}
        </CardContent>
      </Card>

      <OneTimeSecretDialog issued={issued} onClose={() => setIssued(null)} />

      <TypedConfirmDialog
        open={target !== null}
        title="Revoke this API key?"
        // The key's own name, so a mistyped target cannot be confirmed by
        // muscle memory the way a generic "revoke" could.
        phrase={target?.name ?? ""}
        pending={revoke.isPending}
        description={
          <>
            <p>
              Anything using{" "}
              <span className="font-mono">{target?.prefix ?? ""}</span> stops
              working on its very next request — both auth paths read
              `revoked_at` on every call, so there is no cache to wait out.
            </p>
            <p>This cannot be undone. Issue a new key instead.</p>
          </>
        }
        confirmLabel="Revoke key"
        onCancel={() => setTarget(null)}
        onConfirm={async () => {
          if (!target) return;
          try {
            await revoke.mutateAsync(target.id);
            toast.success(`Revoked “${target.name}”`);
          } catch (error) {
            toast.error(errorMessage(error));
          } finally {
            setTarget(null);
          }
        }}
      />
    </div>
  );
}

/**
 * The only place the secret is ever visible. Deliberately has no "close on
 * outside click that also copies" convenience — the operator must acknowledge
 * it, because the alternative to copying now is creating a replacement key.
 */
function OneTimeSecretDialog({
  issued,
  onClose,
}: {
  readonly issued: { name: string; secret: string } | null;
  readonly onClose: () => void;
}) {
  return (
    <Dialog open={issued !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Copy your key now</DialogTitle>
        </DialogHeader>
        <Alert variant="destructive">
          <AlertTitle>This is the only time it will be shown</AlertTitle>
          <AlertDescription>
            <p>
              TITAN stores a hash, not the key. Once you close this dialog the
              value is gone for good — nobody, including an owner, can retrieve
              it. If you lose it, revoke this key and create another.
            </p>
          </AlertDescription>
        </Alert>
        <div className="space-y-2">
          <Label htmlFor="issued-secret">
            {issued ? `Key for “${issued.name}”` : "Key"}
          </Label>
          <Input
            id="issued-secret"
            readOnly
            value={issued?.secret ?? ""}
            className="font-mono text-xs"
            onFocus={(event) => event.currentTarget.select()}
          />
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              if (!issued) return;
              // Clipboard needs a secure context and permission; a silent
              // failure here would look like the value had been copied.
              void navigator.clipboard
                .writeText(issued.secret)
                .then(() => toast.success("Key copied"))
                .catch(() =>
                  toast.error(
                    "Couldn’t copy — select the value and copy it manually.",
                  ),
                );
            }}
          >
            Copy
          </Button>
          <Button onClick={onClose}>I’ve saved it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
