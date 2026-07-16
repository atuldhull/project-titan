"use client";

import { create } from "zustand";

import type { NotificationOut } from "@titan/api-client";

/**
 * The Attention Queue (Vol 4 §6.1, §7.1): items requiring human action
 * (`approval`, `security`). They are NEVER auto-dismissed and are mirrored to
 * the bell. Server-persisted unread notifications seed it on load so the queue
 * survives a reload; live `approval`/`security` frames append to it.
 */
interface AttentionState {
  items: NotificationOut[];
  add: (item: NotificationOut) => void;
  remove: (item: NotificationOut) => void;
  /** Merge the server's unread page in, keeping live items it doesn't contain. */
  seed: (serverItems: NotificationOut[]) => void;
}

export const useAttentionStore = create<AttentionState>((set) => ({
  items: [],
  add: (item) =>
    set((s) =>
      s.items.some((i) => i.id === item.id) ? s : { items: [item, ...s.items] },
    ),
  remove: (item) =>
    set((s) => ({ items: s.items.filter((i) => i.id !== item.id) })),
  seed: (serverItems) =>
    set((s) => {
      // MERGE, don't replace. The seed is the first 50 unread rows (newest
      // first; the API has no category filter), so a pending approval can be
      // pushed off that page by a burst of unrelated notifications — replacing
      // would make it silently vanish from the queue. Items leave only via an
      // explicit resolve, so nothing needing action is dropped.
      const byId = new Map(serverItems.map((i) => [i.id, i]));
      for (const existing of s.items) {
        if (!byId.has(existing.id)) {
          byId.set(existing.id, existing);
        }
      }
      return { items: [...byId.values()] };
    }),
}));
