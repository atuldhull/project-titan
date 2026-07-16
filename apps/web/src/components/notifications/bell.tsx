"use client";

import { useState } from "react";
import { Bell as BellIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useUnreadCount } from "@/lib/notifications/hooks";
import { NotificationCenter } from "./notification-center";

/**
 * Top-bar notification bell (Vol 4 §7.1). The unread badge is server-persisted
 * so it survives a reload, and cross-tab mark-read syncs it in ≤ 2 s
 * (TITAN-FRT-038). Opens the Notification Center popover.
 */
export function Bell() {
  const [open, setOpen] = useState(false);
  const { data, isError } = useUnreadCount();
  const count = data?.count ?? 0;
  const label = count > 0 ? (data?.hasMore ? "50+" : String(count)) : null;
  // Don't report "no unread" when the count simply failed to load — say so.
  const ariaLabel = isError
    ? "Notifications, unread count unavailable"
    : count > 0
      ? `Notifications, ${data?.hasMore ? "over " : ""}${count} unread`
      : "Notifications";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="relative"
          aria-label={ariaLabel}
        >
          <BellIcon aria-hidden className="size-4" />
          {isError ? (
            <span
              aria-hidden
              title="Unread count unavailable"
              className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-warning"
            />
          ) : label ? (
            <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] leading-none font-medium text-white">
              {label}
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <NotificationCenter onNavigate={() => setOpen(false)} />
      </PopoverContent>
    </Popover>
  );
}
