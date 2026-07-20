"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useCan } from "@/lib/auth";
import { cn } from "@/lib/utils";

/**
 * Settings sub-navigation (Vol 4 §6.13).
 *
 * RBAC hides what a role cannot reach (TITAN-FRT-037's UI half). The server
 * re-checks every one of these on the request, so hiding is a courtesy, never
 * the control — and `useCan` reads ORG-WIDE roles only, so it can hide a link
 * the server would have allowed but never reveal one it would deny.
 */
interface NavItem {
  readonly href: string;
  readonly label: string;
  /** Permission required to reach the page, or null when any signed-in user
   *  may (Profile, Appearance, API Keys and Notifications are self-scoped —
   *  authorised by BEING the principal, not by holding a permission). */
  readonly can: string | null;
}

const ITEMS: readonly NavItem[] = [
  { href: "/settings/profile", label: "Profile", can: null },
  { href: "/settings/appearance", label: "Appearance", can: null },
  { href: "/settings/providers", label: "LLM Providers", can: "settings:read" },
  { href: "/settings/members", label: "Members & Roles", can: "member:read" },
  { href: "/settings/api-keys", label: "API Keys", can: null },
  { href: "/settings/notifications", label: "Notifications", can: null },
  {
    href: "/settings/budgets",
    label: "Budgets & Limits",
    can: "settings:read",
  },
];

export function SettingsNav() {
  const pathname = usePathname();
  // Hooks cannot be called conditionally, so both permissions are resolved up
  // front and the list is filtered against them.
  const canSettings = useCan("settings:read");
  const canMembers = useCan("member:read");

  const allowed = ITEMS.filter((item) => {
    if (item.can === "settings:read") return canSettings;
    if (item.can === "member:read") return canMembers;
    return true;
  });

  return (
    <nav aria-label="Settings" className="lg:w-56 lg:shrink-0">
      <h1 className="mb-3 text-lg font-semibold">Settings</h1>
      {/* Horizontal scroller under lg so the seven tabs stay reachable at
          375px without the body itself scrolling. `relative` anchors the fade
          below. */}
      <div className="relative">
        <ul className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible lg:pb-0">
          {allowed.map((item) => {
            const active = pathname === item.href;
            return (
              <li key={item.href} className="shrink-0">
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    // §8: "touch targets ≥ 44 px under md". `px-3 py-2 text-sm`
                    // is ~36px, so the minimum is set explicitly rather than
                    // hoped for from the padding. Held until `lg` rather than
                    // `md`, because 768–1023 is where the tablets are and the
                    // strip is still horizontal and thumb-driven there; at lg it
                    // becomes a pointer-driven vertical sidebar and the extra
                    // height is just slack.
                    "flex min-h-11 items-center rounded-md px-3 text-sm whitespace-nowrap transition-colors lg:block lg:min-h-0 lg:py-2",
                    active
                      ? "bg-secondary font-medium text-secondary-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  )}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
        {/*
         * The scroll affordance. Without it the strip is a dead end: the seven
         * items overflow at 375px, the last one is cut off at the exact edge of
         * the viewport, and nothing about a flush-cut label says "there is more
         * this way" — overlay scrollbars are invisible until you already scroll.
         * A fade off the right edge is the standard cue and costs no layout.
         *
         * `pointer-events-none` so it never eats a tap on the item beneath it,
         * `aria-hidden` because it is pure decoration, and `lg:hidden` because
         * at lg the list is vertical and no longer scrolls.
         */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-background to-transparent lg:hidden"
        />
      </div>
    </nav>
  );
}
