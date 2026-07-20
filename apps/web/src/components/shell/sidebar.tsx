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
 * 240px sidebar collapsible to a 64px icon rail (Vol 4 §2.3, §8).
 *
 * TWO things can put it in rail mode and they compose:
 *  - the BREAKPOINT — §8 mandates a rail from 768 to 1279, full only at ≥1280;
 *  - the USER's manual collapse, which lives on <html data-sidebar>, is applied
 *    pre-paint by the root inline script and persisted to localStorage.
 *
 * Both are pure CSS. A JS breakpoint read (matchMedia) would have to run after
 * hydration, so the first paint would show the wrong width on every load — and
 * the app has no matchMedia anywhere else to model it on.
 *
 * Labels stay in the DOM as `sr-only` rather than `hidden` in rail mode, and
 * every link carries an explicit aria-label, so an icon rail is still fully
 * navigable by screen reader (TITAN-FRT-042/043).
 */

/** Visible only when the sidebar is full-width: ≥1280 AND not manually collapsed. */
const LABEL = "max-xl:hidden [[data-sidebar=collapsed]_&]:hidden";
/** Rail geometry: centred, no padding. Applies below xl, or when collapsed. */
const RAIL_ITEM =
  "max-xl:justify-center max-xl:px-0 [[data-sidebar=collapsed]_&]:justify-center [[data-sidebar=collapsed]_&]:px-0";

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
      // The visible label is display:none in rail mode, which removes it from
      // the a11y tree too — so the name has to come from somewhere that cannot
      // be hidden, or every rail link announces as "link".
      aria-label={item.title}
      aria-current={current ? "page" : undefined}
      className={cn(
        "flex h-9 items-center gap-3 rounded-md px-2.5 text-muted-foreground",
        "hover:bg-accent hover:text-foreground",
        RAIL_ITEM,
        active && "bg-accent font-medium text-foreground",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      <span className={cn("truncate", LABEL)}>{item.title}</span>
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
        // §8: rail from md, full width only from xl (1280).
        "hidden w-16 shrink-0 flex-col border-r bg-card md:flex xl:w-60",
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
                "truncate px-2.5 py-1 font-mono text-xs text-muted-foreground",
                LABEL,
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
      {/* Below xl the rail is mandated by §8, so a toggle there would promise a
          width the breakpoint immediately takes back. */}
      <div className="border-t p-2 max-xl:hidden">
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
          <span className={LABEL}>Collapse</span>
        </button>
      </div>
    </aside>
  );
}
