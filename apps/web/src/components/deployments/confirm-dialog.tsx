"use client";

/**
 * The confirmation §6.9/FRT-032 requires: restate the scope, and for a
 * protected environment make the operator type its name.
 *
 * Generic on purpose (the phase brief: "implement the confirmation/RBAC gate
 * logic generically so later environments slot in"). It renders a policy; it
 * does not decide one — `lib/deployments/gate` does that, and is tested.
 */
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  confirmationMatches,
  type Environment,
  type GatePolicy,
} from "@/lib/deployments/gate";

export function ConfirmDeployDialog({
  open,
  policy,
  environment,
  description,
  onCancel,
  onConfirm,
}: {
  readonly open: boolean;
  readonly policy: GatePolicy;
  readonly environment: Environment;
  readonly description: string;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");

  // Clear between openings, or a confirmation typed for the LAST action would
  // still be sitting there pre-satisfying the next one.
  useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  const satisfied =
    !policy.requiresTypedName || confirmationMatches(typed, environment);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm">
            {policy.scope}
          </DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {policy.requiresTypedName ? (
          <div className="space-y-2">
            <label
              htmlFor="confirm-env"
              className="text-xs text-muted-foreground"
            >
              Type{" "}
              <code className="font-mono font-semibold">{environment}</code> to
              confirm.
            </label>
            <Input
              id="confirm-env"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              // Autofill would defeat the whole control: the point of typing is
              // that the operator reads the word.
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
            />
          </div>
        ) : null}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={!satisfied}
            onClick={onConfirm}
          >
            Confirm
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
