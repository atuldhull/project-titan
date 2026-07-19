"use client";

import { useEffect } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { SchemaFields, useSchemaForm } from "@/components/plugins/schema-form";
import { initialValues, toSubmitPayload } from "@/lib/forms/json-schema";
import { usePluginConfig, useSavePluginConfig } from "@/lib/plugins/hooks";
import { errorMessage } from "@/lib/settings/errors";

/**
 * The per-plugin config form (TITAN-FRT-035).
 *
 * Everything rendered here comes from the plugin's own JSON Schema — no branch
 * on plugin id anywhere. Two behaviours are worth stating because getting
 * either wrong is silent and destructive:
 *
 * * **A saved secret arrives as `••••` and is submitted back unchanged.** The
 *   server recognises the mask as "leave as it is". If the client stripped it
 *   instead, an unchanged form would clear a working credential; if the server
 *   stored it verbatim, the credential would become four bullet characters.
 * * **Manifest-declared secrets are NOT editable.** They resolve from the host
 *   environment (Vol 8 SEC-050), so the dialog reports whether each resolves
 *   and names the variable — it never offers a field that could not persist.
 */
export function PluginConfigDialog({
  pluginId,
  open,
  onClose,
}: {
  readonly pluginId: string | null;
  readonly open: boolean;
  readonly onClose: () => void;
}) {
  const config = usePluginConfig(open ? pluginId : null);
  const save = useSavePluginConfig(pluginId ?? "");
  const { model, form } = useSchemaForm(config.data?.config_schema, {});
  const { reset } = form;

  // Seed once the server answers. `reset` sets the dirty BASELINE to the saved
  // values, so Save stays disabled until something actually changes — and the
  // masked secret counts as unchanged, which is what makes "save without
  // touching the secret" a no-op for that field.
  useEffect(() => {
    if (config.data) {
      reset(initialValues(model, config.data.values ?? {}));
    }
  }, [config.data, model, reset]);

  const secrets = config.data?.secrets ?? [];

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Configure {pluginId}</DialogTitle>
          <DialogDescription>
            Generated from this plugin’s manifest schema. Values are encrypted
            at rest.
          </DialogDescription>
        </DialogHeader>

        {config.isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : config.isError ? (
          <p role="status" className="text-sm text-muted-foreground">
            Couldn’t load the config form. {errorMessage(config.error)}
          </p>
        ) : (
          <form
            id="plugin-config-form"
            className="space-y-5"
            onSubmit={form.handleSubmit(async (values) => {
              try {
                await save.mutateAsync(
                  // Pass the saved config so keys this form could not render
                  // are carried through rather than deleted — `configure`
                  // replaces the stored document wholesale.
                  toSubmitPayload(model, values, config.data?.values ?? {}),
                );
                reset(values);
                toast.success("Configuration saved");
                onClose();
              } catch (error) {
                toast.error(errorMessage(error));
              }
            })}
          >
            <SchemaFields model={model} form={form} disabled={save.isPending} />

            {secrets.length > 0 ? (
              <div className="space-y-2 rounded-md border border-dashed p-3">
                <p className="text-xs font-medium">Secrets this plugin needs</p>
                <p className="text-xs text-muted-foreground">
                  TITAN never stores these. Set the environment variable on the
                  host and restart the API.
                </p>
                <ul className="space-y-1">
                  {secrets.map((secret) => (
                    <li
                      key={secret.name}
                      className="flex items-center justify-between gap-2 text-xs"
                    >
                      <span className="font-mono">{secret.env_var}</span>
                      <Badge
                        variant={secret.resolved ? "secondary" : "outline"}
                      >
                        {secret.resolved ? "✔ set" : "✖ not set"}
                      </Badge>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </form>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={save.isPending}>
            Close
          </Button>
          <Button
            type="submit"
            form="plugin-config-form"
            disabled={
              !form.formState.isDirty || save.isPending || config.isLoading
            }
          >
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
