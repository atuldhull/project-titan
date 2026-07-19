"use client";

import { useEffect, useState, type ReactNode } from "react";

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
import { Label } from "@/components/ui/label";

/**
 * Typed-confirmation for a destructive Settings action (Vol 4 §6.13:
 * "destructive actions (remove member, revoke key) use typed-confirmation
 * dialogs").
 *
 * Separate from `deployments/confirm-dialog` on purpose: that one renders a
 * deployment GATE POLICY (environment, RBAC verdict, protected-environment
 * rules) decided by `lib/deployments/gate`. This one takes a plain phrase.
 * Merging them would drag deploy-policy types into member management.
 */
export function TypedConfirmDialog({
  open,
  title,
  description,
  phrase,
  confirmLabel = "Confirm",
  pending = false,
  onCancel,
  onConfirm,
}: {
  readonly open: boolean;
  readonly title: string;
  readonly description: ReactNode;
  /** What the operator must type, exactly. Case-sensitive. */
  readonly phrase: string;
  readonly confirmLabel?: string;
  readonly pending?: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");

  // Reset when the dialog opens for a DIFFERENT target. Without this, closing
  // the dialog for member A and opening it for member B would arrive with A's
  // phrase already satisfied and the destructive button live on first paint.
  useEffect(() => {
    if (open) setTyped("");
  }, [open, phrase]);

  const matches = typed === phrase;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-2 text-sm">{description}</div>
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="typed-confirmation">
            Type <span className="font-mono font-medium">{phrase}</span> to
            confirm
          </Label>
          <Input
            id="typed-confirmation"
            value={typed}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setTyped(event.target.value)}
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches || pending}
            onClick={onConfirm}
          >
            {pending ? "Working…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
