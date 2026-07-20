"use client";

import Link from "next/link";
import { CircleUser, Menu, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { MobileNav } from "@/components/shell/mobile-nav";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { ConnectionPill } from "@/components/shell/connection-pill";
import { ProjectSwitcherButton } from "@/components/shell/project-switcher";
import { usePaletteOpener } from "@/components/shell/command-palette";
import { Bell } from "@/components/notifications/bell";

/*
 * 56px top bar (Vol 4 §2.3): project switcher (⌘P), command-palette trigger
 * (⌘K), WS connection pill, notification bell, user menu.
 *
 * The switcher and palette buttons stopped being stubs in Phase 65 — both now
 * open the real palette. They stay as visible buttons rather than being folded
 * into the keyboard shortcut alone: §8 puts the whole dashboard on a 375px
 * touch screen, where there is no ⌘K to press.
 */
export function TopBar() {
  const openPalette = usePaletteOpener();
  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b bg-card px-3 md:px-4">
      <Sheet>
        <SheetTrigger asChild className="md:hidden">
          <Button variant="ghost" size="icon" aria-label="Open navigation">
            <Menu aria-hidden className="size-5" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="border-b px-4 py-3">
            <SheetTitle className="text-left text-sm">TITAN</SheetTitle>
          </SheetHeader>
          <MobileNav />
        </SheetContent>
      </Sheet>

      <Link
        href="/org"
        className="mr-1 hidden items-center gap-2 font-semibold md:flex"
      >
        <span
          aria-hidden
          className="inline-block size-2.5 rounded-full bg-primary"
        />
        TITAN
      </Link>

      <ProjectSwitcherButton onOpen={() => openPalette("projects")} />

      <Button
        variant="outline"
        size="sm"
        onClick={() => openPalette("commands")}
        aria-label="Search screens, projects, agents and tasks"
        aria-keyshortcuts="Control+K Meta+K"
        className="ml-auto justify-start gap-2 text-muted-foreground md:w-56"
      >
        <Search aria-hidden className="size-3.5" />
        <span className="hidden md:inline">Search…</span>
        <Kbd className="ml-auto hidden md:inline-flex">⌘K</Kbd>
      </Button>

      <ConnectionPill />

      <Bell />

      {/*
       * Hidden below md. §3.2 names Settings → Appearance as the theme's
       * canonical home, so this is a convenience copy — and at 375px it is the
       * control that will not fit: seven controls share the bar, and giving
       * this one §8's 44px minimum pushed the page 3px wider than the viewport,
       * i.e. produced exactly the horizontal body scroll §8 forbids. Dropping a
       * duplicate is the honest resolution; shrinking it below 44px would just
       * move the violation.
       */}
      <ThemeToggle className="max-md:hidden" />

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="User menu">
            <CircleUser aria-hidden className="size-5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48">
          <DropdownMenuLabel>Local admin</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link href="/settings/profile">Settings</Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link href="/login">Sign in</Link>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  );
}
