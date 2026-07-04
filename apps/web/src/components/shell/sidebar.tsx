"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { PanelLeftClose } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  orgNavItems,
  projectNavItems,
  type NavItem,
} from "@/components/shell/nav-items";

/*
 * 240px sidebar collapsible to a 64px icon rail (Vol 4 §2.3). Collapse state
 * lives on <html data-sidebar> — applied pre-paint by the root inline script
 * and persisted to localStorage — so width is pure CSS and SSR/CSR markup
 * never diverges. The `[[data-sidebar=collapsed]_&]` variants react to it.
 */

const COLLAPSED_HIDDEN = "[[data-sidebar=collapsed]_&]:hidden";

function toggleCollapsed() {
  const root = document.documentElement;
  const collapsed = root.dataset.sidebar === "collapsed";
  if (collapsed) {
    delete root.dataset.sidebar;
    localStorage.setItem("titan.sidebar", "expanded");
  } else {
    root.dataset.sidebar = "collapsed";
    localStorage.setItem("titan.sidebar", "collapsed");
  }
}

function NavLink({
  item,
  active,
  current = active,
}: {
  item: NavItem;
  /** Visual highlight (section match). */
  active: boolean;
  /** Exact page match — only this link gets aria-current="page". */
  current?: boolean;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      title={item.title}
      aria-current={current ? "page" : undefined}
      className={cn(
        "flex h-9 items-center gap-3 rounded-md px-2.5 text-muted-foreground",
        "hover:bg-accent hover:text-foreground",
        "[[data-sidebar=collapsed]_&]:justify-center [[data-sidebar=collapsed]_&]:px-0",
        active && "bg-accent font-medium text-foreground",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      <span className={cn("truncate", COLLAPSED_HIDDEN)}>{item.title}</span>
    </Link>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const pid = /^\/projects\/([^/]+)/.exec(pathname)?.[1];
  const projectItems = pid ? projectNavItems(pid) : [];

  const isActive = (href: string) =>
    pathname === href ||
    (href !== "/settings/profile" && pathname.startsWith(`${href}/`));

  return (
    <aside
      className={cn(
        "hidden w-60 shrink-0 flex-col border-r bg-card md:flex",
        "[[data-sidebar=collapsed]_&]:w-16",
      )}
    >
      <nav
        aria-label="Primary"
        className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2"
      >
        {orgNavItems.slice(0, 2).map((item) => (
          <NavLink
            key={item.href}
            item={item}
            active={isActive(item.href)}
            current={pathname === item.href}
          />
        ))}
        {projectItems.length > 0 && (
          <div
            className="my-1 border-t pt-1"
            role="group"
            aria-label="Current project"
          >
            <p
              className={cn(
                "px-2.5 py-1 font-mono text-xs text-muted-foreground",
                COLLAPSED_HIDDEN,
              )}
            >
              {pid}
            </p>
            {projectItems.map((item) => (
              <NavLink
                key={item.href}
                item={item}
                active={pathname === item.href}
              />
            ))}
            <div className="mt-1 border-t" />
          </div>
        )}
        {orgNavItems.slice(2).map((item) => (
          <NavLink
            key={item.href}
            item={item}
            active={
              isActive(item.href) ||
              (item.href === "/settings/profile" &&
                pathname.startsWith("/settings"))
            }
            current={pathname === item.href}
          />
        ))}
      </nav>
      <div className="border-t p-2">
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label="Toggle sidebar"
          className={cn(
            "flex h-9 w-full items-center gap-3 rounded-md px-2.5 text-muted-foreground",
            "hover:bg-accent hover:text-foreground",
            "[[data-sidebar=collapsed]_&]:justify-center [[data-sidebar=collapsed]_&]:px-0",
          )}
        >
          <PanelLeftClose
            aria-hidden
            className="size-4 shrink-0 [[data-sidebar=collapsed]_&]:rotate-180"
          />
          <span className={COLLAPSED_HIDDEN}>Collapse</span>
        </button>
      </div>
    </aside>
  );
}
