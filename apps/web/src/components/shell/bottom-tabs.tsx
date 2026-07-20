"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  Building2,
  FolderKanban,
  MessagesSquare,
  MoreHorizontal,
} from "lucide-react";

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { MobileNav } from "@/components/shell/mobile-nav";
import { cn } from "@/lib/utils";

/**
 * The `< 768` bottom tab bar (Vol 4 §8): Org / Board / Agents / Convos / More.
 *
 * Fixed to the bottom, so `(dashboard)/layout.tsx` pads `<main>` by the same
 * height — padding the body would do nothing, because `<main>` is the scroll
 * port, and without it the last row of every screen sits under the bar.
 *
 * Every target is 44px+ (§8) and the bar is inside its own `<nav>` with a
 * distinct label, so it does not collide with the Sheet's "Primary" landmark.
 */

/** §8 names five slots; Board is project-scoped and falls back to the picker. */
const TABS = [
  { title: "Org", href: () => "/org", icon: Building2 },
  {
    title: "Board",
    href: (pid?: string) => (pid ? `/projects/${pid}/board` : "/projects"),
    icon: FolderKanban,
  },
  { title: "Agents", href: () => "/agents", icon: Bot },
  { title: "Convos", href: () => "/conversations", icon: MessagesSquare },
] as const;

export function BottomTabs() {
  const pathname = usePathname();
  const pid = /^\/projects\/([^/]+)/.exec(pathname)?.[1];

  return (
    <nav
      aria-label="Sections"
      className="fixed inset-x-0 bottom-0 z-40 flex border-t bg-card md:hidden"
    >
      {TABS.map((tab) => {
        const href = tab.href(pid);
        const Icon = tab.icon;
        const active = pathname === href;
        return (
          <Link
            key={tab.title}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] text-muted-foreground",
              active && "text-foreground",
            )}
          >
            <Icon aria-hidden className="size-5" />
            {tab.title}
          </Link>
        );
      })}
      <Sheet>
        <SheetTrigger className="flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] text-muted-foreground">
          <MoreHorizontal aria-hidden className="size-5" />
          More
        </SheetTrigger>
        <SheetContent side="bottom" className="max-h-[80dvh] overflow-y-auto">
          <SheetHeader className="px-4 py-3">
            <SheetTitle className="text-left text-sm">All sections</SheetTitle>
          </SheetHeader>
          <MobileNav />
        </SheetContent>
      </Sheet>
    </nav>
  );
}
