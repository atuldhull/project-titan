"use client";

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import {
  broadcastNotification,
  subscribeNotificationBroadcast,
} from "./broadcast";

/** Notification history (Center + Attention seeding). */
export function useNotifications(unread = false) {
  return useQuery({
    queryKey: queryKeys.notifications({ unread }),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/notifications", {
          params: { query: { unread, limit: 50 } },
        }),
      ),
  });
}

/**
 * The bell badge count. There is no dedicated count endpoint, so we read the
 * first page of unread; `hasMore` drives a "50+" display. Server-persisted, so
 * it survives a reload (TITAN-FRT-038).
 */
export function useUnreadCount() {
  return useQuery({
    queryKey: queryKeys.notificationsUnread(),
    queryFn: async () => {
      const page = unwrap(
        await browserApi().GET("/api/v1/notifications", {
          params: { query: { unread: true, limit: 50 } },
        }),
      );
      return { count: page.items.length, hasMore: page.next_cursor != null };
    },
  });
}

function invalidateNotifications(qc: ReturnType<typeof useQueryClient>): void {
  void qc.invalidateQueries({ queryKey: queryKeys.notifications() });
  void qc.invalidateQueries({ queryKey: queryKeys.notificationsUnread() });
}

export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await browserApi().POST(
          "/api/v1/notifications/{notification_id}:mark-read",
          { params: { path: { notification_id: id } } },
        ),
      ),
    onSuccess: () => {
      invalidateNotifications(qc);
      broadcastNotification({ type: "read" });
    },
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await browserApi().POST("/api/v1/notifications:mark-all-read")),
    onSuccess: () => {
      invalidateNotifications(qc);
      broadcastNotification({ type: "read-all" });
    },
  });
}

/**
 * Keep unread state in sync across tabs (TITAN-FRT-038): a mark-read elsewhere
 * invalidates this tab's notification queries, so the badge converges in ≤ 2 s.
 */
export function useNotificationBroadcastSync(): void {
  const qc = useQueryClient();
  useEffect(
    () => subscribeNotificationBroadcast(() => invalidateNotifications(qc)),
    [qc],
  );
}
