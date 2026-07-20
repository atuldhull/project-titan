"use client";

import { create } from "zustand";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { byGroup, caps } from "@/lib/shortcuts/registry";
import { useShortcutTable } from "@/lib/shortcuts/provider";

/**
 * The `?` shortcut reference (TITAN-FRT-048).
 *
 * Rendered FROM THE LIVE REGISTRY, not from a hand-written copy of §10's table.
 * The requirement is that the overlay match what actually works, and the only
 * way to keep those two in step over time is for them to be the same data — a
 * transcribed table drifts the first time a binding is renamed.
 *
 * A consequence worth knowing: the overlay lists what is registered *right now*,
 * so screen-scoped rows (Board, Graph, Logs, Conversations) appear only on their
 * own screens. That is the honest reading of "every binding" — showing Board
 * keys on Settings would advertise something that does nothing there.
 */

interface ShortcutsOverlayState {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

export const useShortcutsOverlay = create<ShortcutsOverlayState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
  toggle: () => set((state) => ({ open: !state.open })),
}));

export function ShortcutsOverlay() {
  const open = useShortcutsOverlay((state) => state.open);
  const setOpen = useShortcutsOverlay((state) => state.setOpen);
  const { all, mac } = useShortcutTable();
  const sections = byGroup(all);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>
            Screen-specific shortcuts are listed while you are on that screen.
            Sequences like <Kbd>G</Kbd> <Kbd>O</Kbd> time out after 1.5 seconds.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-6 sm:grid-cols-2">
          {sections.map((section) => (
            <section key={section.group} className="space-y-1">
              <h3 className="pb-1 text-xs font-medium text-muted-foreground">
                {section.group}
              </h3>
              <dl className="space-y-1">
                {section.shortcuts.map((shortcut) => (
                  <div
                    key={shortcut.id}
                    data-shortcut={shortcut.id}
                    className="flex items-baseline justify-between gap-3"
                  >
                    <dt className="text-sm">{shortcut.label}</dt>
                    <dd className="flex shrink-0 items-center gap-1">
                      {caps(shortcut.keys, mac).map((step, index) => (
                        <span
                          // Steps are positional; a sequence can legitimately
                          // repeat a cap (`g` then `g`).
                          key={index}
                          className="flex items-center gap-0.5"
                        >
                          {step.map((cap) => (
                            <Kbd key={cap}>{cap}</Kbd>
                          ))}
                        </span>
                      ))}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
