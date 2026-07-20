"use client";

import { useEffect, type RefObject } from "react";
import { create } from "zustand";

/**
 * The `/` target for the current screen (Vol 4 §10: "focus the screen's primary
 * search/filter").
 *
 * A store rather than a context lookup because the shortcut fires in the shell
 * while the input lives several levels down in a screen, and the shell must not
 * import screen components to reach it (TITAN-FRT-049 forbids exactly that).
 *
 * Screens with no search simply never register, and `/` falls back to opening
 * the palette — which IS a search, so the key never feels dead.
 */
interface PrimarySearchState {
  focus: (() => void) | null;
  set: (focus: (() => void) | null) => void;
}

const usePrimarySearchStore = create<PrimarySearchState>((set) => ({
  focus: null,
  set: (focus) => set({ focus }),
}));

/**
 * Register this screen's search input as the `/` target for as long as the
 * component is mounted.
 */
export function usePrimarySearch(
  ref: RefObject<HTMLInputElement | HTMLTextAreaElement | null>,
): void {
  const set = usePrimarySearchStore((state) => state.set);
  useEffect(() => {
    const focus = () => {
      const element = ref.current;
      if (!element) {
        return;
      }
      element.focus();
      element.select();
    };
    set(focus);
    // Clearing on unmount matters: navigating away would otherwise leave `/`
    // pointed at a detached input, silently doing nothing.
    return () => set(null);
  }, [ref, set]);
}

export function usePrimarySearchFocus(): (() => void) | null {
  return usePrimarySearchStore((state) => state.focus);
}
