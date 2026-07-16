"use client";

import { useEffect } from "react";
import Link from "next/link";
import { toast } from "sonner";
import { CheckCircle2, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAttentionStore } from "@/lib/notifications/attention-store";
import { routeNotification } from "@/lib/notifications/taxonomy";
import { useMarkRead, useNotifications } from "@/lib/notifications/hooks";
import type { NotificationOut } from "@titan/api-client";

/**
 * The Attention Queue (Vol 4 §6.1, §7.1): items requiring human action. Never
 * auto-dismissed; mirrored to the bell. Seeded from server-persisted unread so
 * it survives a reload, and appended to live by the WS bus.
 *
 * Membership is decided by `routeNotification`, not a hardcoded category list,
 * so backend categories outside the §7.1 vocabulary (e.g. `pipeline`, `plugin`)
 * still land here when they are critical.
 */
function AttentionRow({ item }: { item: NotificationOut }) {
  // A mutation per row: one shared instance would disable every Resolve button
  // whenever any single one is in flight.
  const markRead = useMarkRead();
  const remove = useAttentionStore((s) => s.remove);
  const add = useAttentionStore((s) => s.add);

  const resolve = () => {
    remove(item); // optimistic
    markRead.mutate(item.id, {
      onError: () => {
        // Roll back — an unresolved approval must never silently disappear
        // (TITAN-FRT-015).
        add(item);
        toast.error("Couldn’t resolve that item.", {
          action: { label: "Retry", onClick: resolve },
        });
      },
    });
  };

  return (
    <li className="flex items-start gap-3 rounded-lg border bg-card p-3">
      <ShieldAlert
        aria-hidden
        className="mt-0.5 size-4 shrink-0 text-warning"
      />
      <div className="min-w-0 flex-1">
        <Badge variant="destructive" className="capitalize">
          {item.category}
        </Badge>
        <p className="mt-1 text-sm font-medium">
          {item.link ? (
            <Link href={item.link} className="hover:underline">
              {item.title}
            </Link>
          ) : (
            item.title
          )}
        </p>
        {item.body ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{item.body}</p>
        ) : null}
      </div>
      <Button
        variant="outline"
        size="sm"
        disabled={markRead.isPending}
        onClick={resolve}
      >
        Resolve
      </Button>
    </li>
  );
}

export function AttentionQueue() {
  const { data, isLoading, isError } = useNotifications(true);
  const items = useAttentionStore((s) => s.items);
  const seed = useAttentionStore((s) => s.seed);

  useEffect(() => {
    if (!data) {
      return;
    }
    seed(
      data.items.filter(
        (n) => routeNotification(n.category, n.severity).attentionQueue,
      ),
    );
  }, [data, seed]);

  if (isLoading) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        Loading items needing attention…
      </div>
    );
  }
  // Never render the all-clear on an error — that would tell the user nothing
  // needs them while approvals are pending (TITAN-FRT-002).
  if (isError) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center text-sm text-destructive">
        Couldn’t load items needing attention.
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        <CheckCircle2 aria-hidden className="size-5 text-success" />
        Nothing needs your attention.
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <AttentionRow key={item.id} item={item} />
      ))}
    </ul>
  );
}
