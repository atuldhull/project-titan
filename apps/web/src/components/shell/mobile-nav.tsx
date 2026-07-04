"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";
import { orgNavItems, projectNavItems } from "@/components/shell/nav-items";

/** Drawer navigation for < md viewports (Vol 4 §8 — usable at 375px). */
export function MobileNav() {
  const pathname = usePathname();
  const pid = /^\/projects\/([^/]+)/.exec(pathname)?.[1];
  const items = pid
    ? [
        ...orgNavItems.slice(0, 2),
        ...projectNavItems(pid),
        ...orgNavItems.slice(2),
      ]
    : orgNavItems;

  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 p-2">
      {items.map((item) => {
        const Icon = item.icon;
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex h-11 items-center gap-3 rounded-md px-3 text-muted-foreground",
              "hover:bg-accent hover:text-foreground",
              active && "bg-accent font-medium text-foreground",
            )}
          >
            <Icon aria-hidden className="size-4 shrink-0" />
            {item.title}
          </Link>
        );
      })}
    </nav>
  );
}
