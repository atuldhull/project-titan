"use client";

import { useState } from "react";
import Link from "next/link";
import { Check, CheckCheck, Inbox } from "lucide-react";

import type { NotificationOut } from "@titan/api-client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useMarkAllRead,
  useMarkRead,
  useNotifications,
} from "@/lib/notifications/hooks";
import { isCritical } from "@/lib/notifications/taxonomy";
import { cn } from "@/lib/utils";

function timeAgo(iso: string, nowMs: number = Date.now()): string {
  const seconds = Math.round((nowMs - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function categoryVariant(
  category: string,
  severity: string,
): "default" | "secondary" | "destructive" | "outline" {
  if (isCritical(severity)) return "destructive";
  if (category === "failure") return "destructive";
  if (category === "progress") return "secondary";
  return "outline";
}

function NotificationRow({
  notification,
  onNavigate,
}: {
  notification: NotificationOut;
  onNavigate?: () => void;
}) {
  const markRead = useMarkRead();
  const unread = notification.read_at === null;
  return (
    <li
      className={cn(
        "flex gap-2 border-b px-3 py-2.5 last:border-b-0",
        unread && "bg-accent/40",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "mt-1.5 size-1.5 shrink-0 rounded-full",
          unread ? "bg-primary" : "bg-transparent",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Badge
            variant={categoryVariant(
              notification.category,
              notification.severity,
            )}
            className="capitalize"
          >
            {notification.category}
          </Badge>
          <span className="ml-auto text-xs text-muted-foreground">
            {timeAgo(notification.created_at)}
          </span>
        </div>
        <p className="mt-1 truncate text-sm font-medium">
          {notification.link ? (
            <Link
              href={notification.link}
              className="hover:underline"
              onClick={onNavigate}
            >
              {notification.title}
            </Link>
          ) : (
            notification.title
          )}
        </p>
        {notification.body ? (
          <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
            {notification.body}
          </p>
        ) : null}
      </div>
      {unread ? (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Mark read"
          title="Mark read"
          disabled={markRead.isPending}
          onClick={() => markRead.mutate(notification.id)}
        >
          <Check aria-hidden />
        </Button>
      ) : null}
    </li>
  );
}

/**
 * The Notification Center popover content (Vol 4 §7.1): history, an unread
 * filter, mark-read / mark-all-read, and deep links. Renders the four distinct
 * states required by TITAN-FRT-002.
 */
export function NotificationCenter({
  onNavigate,
}: {
  onNavigate?: () => void;
}) {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const { data, isLoading, isError } = useNotifications(unreadOnly);
  const markAll = useMarkAllRead();
  const items = data?.items ?? [];

  return (
    <div className="flex max-h-[70vh] w-80 flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span className="text-sm font-semibold">Notifications</span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant={unreadOnly ? "secondary" : "ghost"}
            size="xs"
            onClick={() => setUnreadOnly((v) => !v)}
          >
            Unread
          </Button>
          <Button
            variant="ghost"
            size="xs"
            disabled={markAll.isPending}
            onClick={() => markAll.mutate()}
          >
            <CheckCheck aria-hidden />
            Mark all
          </Button>
        </div>
      </div>

      <div className="overflow-y-auto">
        {isLoading ? (
          <p className="px-3 py-8 text-center text-sm text-muted-foreground">
            Loading…
          </p>
        ) : isError ? (
          <p className="px-3 py-8 text-center text-sm text-destructive">
            Couldn’t load notifications.
          </p>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-3 py-8 text-center text-sm text-muted-foreground">
            <Inbox aria-hidden className="size-5" />
            {unreadOnly ? "No unread notifications." : "You’re all caught up."}
          </div>
        ) : (
          <ul>
            {items.map((notification) => (
              <NotificationRow
                key={notification.id}
                notification={notification}
                onNavigate={onNavigate}
              />
            ))}
          </ul>
        )}
      </div>

      <Link
        href="/settings/notifications"
        onClick={onNavigate}
        className="border-t px-3 py-2 text-center text-xs text-muted-foreground hover:text-foreground"
      >
        Notification settings
      </Link>
    </div>
  );
}
