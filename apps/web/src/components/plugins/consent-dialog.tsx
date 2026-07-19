"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { grantsNothing, requestedPermissions } from "@/lib/plugins/derive";
import type { PluginOut } from "@titan/api-client";

/**
 * The permission consent dialog (Vol 4 §6.12; Vol 7 §3.4).
 *
 * §6.12: "each install dialog lists exactly which capabilities/scopes the
 * plugin gets, mirroring the Volume 7 permission model". Two properties of the
 * runtime make completeness matter more than brevity:
 *
 * 1. **Enable grants the whole declared set at once.** There is no
 *    per-capability approval anywhere in the runtime, so this list IS the
 *    consent — an operator who accepts it has accepted all of it.
 * 2. **A plugin's `scopes` become required on EVERY tool it contributes**, not
 *    per tool. Declaring `[task:read, terminal:run]` means a caller must hold
 *    both to call any of them, which is why the scopes are shown verbatim
 *    rather than summarised into a friendly sentence.
 */
export function ConsentDialog({
  plugin,
  open,
  pending,
  onCancel,
  onConfirm,
}: {
  readonly plugin: PluginOut | null;
  readonly open: boolean;
  readonly pending: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}) {
  if (!plugin) return null;
  const permissions = requestedPermissions(plugin);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent className="max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Install {plugin.name}?</DialogTitle>
          <DialogDescription>
            Installing validates the manifest and builds an isolated
            environment. It grants nothing — enabling does that, and you can
            review this again first.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          {grantsNothing(plugin) ? (
            <p className="text-muted-foreground">
              This plugin requests no capabilities at all.
            </p>
          ) : (
            <>
              <p className="font-medium">When enabled it will be granted:</p>
              {permissions.map((permission) => (
                <div key={permission.kind} className="space-y-1">
                  <p className="text-xs font-medium text-muted-foreground">
                    {permission.kind}
                  </p>
                  <ul className="space-y-0.5">
                    {permission.values.map((value) => (
                      <li key={value} className="font-mono text-xs">
                        {value}
                      </li>
                    ))}
                  </ul>
                  {permission.note ? (
                    <p className="text-xs text-muted-foreground">
                      {permission.note}
                    </p>
                  ) : null}
                </div>
              ))}
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={onConfirm} disabled={pending}>
            {pending ? "Installing…" : "Install"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
