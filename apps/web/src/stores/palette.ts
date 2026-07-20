"use client";

import { create } from "zustand";

/**
 * Command-palette / project-switcher open state — pure UI state (Vol 4 §4.1).
 *
 * ONE store for both surfaces rather than two: ⌘K and ⌘P open the same dialog
 * with a different candidate set, and modelling them separately made it
 * possible for both to be open at once — two stacked modals, with Esc closing
 * whichever Radix happened to mount last.
 */
export type PaletteMode = "commands" | "projects";

interface PaletteState {
  mode: PaletteMode | null;
  open: (mode: PaletteMode) => void;
  close: () => void;
  /** Opens `mode`, or closes if that same mode is already showing. */
  toggle: (mode: PaletteMode) => void;
}

export const usePaletteStore = create<PaletteState>((set) => ({
  mode: null,
  open: (mode) => set({ mode }),
  close: () => set({ mode: null }),
  toggle: (mode) =>
    set((state) => ({ mode: state.mode === mode ? null : mode })),
}));
