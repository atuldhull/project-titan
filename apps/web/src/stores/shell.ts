"use client";

import { create } from "zustand";

/**
 * Pure UI shell state (Vol 4 §4.1 — client-only, not server state). The sidebar
 * collapse persists to `localStorage["titan.sidebar"]`, which the root layout's
 * blocking inline script reads pre-paint (mirrored on `data-sidebar`) so there
 * is no layout flash.
 */
const STORAGE_KEY = "titan.sidebar";

function readInitial(): boolean {
  if (typeof document === "undefined") {
    return false;
  }
  return document.documentElement.dataset.sidebar === "collapsed";
}

function persist(collapsed: boolean): void {
  try {
    if (typeof localStorage !== "undefined") {
      if (collapsed) {
        localStorage.setItem(STORAGE_KEY, "collapsed");
      } else {
        localStorage.removeItem(STORAGE_KEY);
      }
    }
    if (typeof document !== "undefined") {
      document.documentElement.dataset.sidebar = collapsed ? "collapsed" : "";
    }
  } catch {
    // storage unavailable (private mode) — degrade to in-memory only
  }
}

interface ShellState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
  setSidebar: (collapsed: boolean) => void;
}

export const useShellStore = create<ShellState>((set) => ({
  sidebarCollapsed: readInitial(),
  toggleSidebar: () =>
    set((s) => {
      const next = !s.sidebarCollapsed;
      persist(next);
      return { sidebarCollapsed: next };
    }),
  setSidebar: (collapsed) => {
    persist(collapsed);
    set({ sidebarCollapsed: collapsed });
  },
}));
