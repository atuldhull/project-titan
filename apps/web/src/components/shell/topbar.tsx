"use client";

import Link from "next/link";
import { ChevronsUpDown, CircleUser, Menu, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
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
import { Bell } from "@/components/notifications/bell";

/*
 * 56px top bar (Vol 4 §2.3): project switcher (⌘P), command-palette trigger
 * (⌘K), WS connection pill, notification bell, user menu. Switcher, palette,
 * pill, and bell are visual stubs until their backing features land (palette
 * M6, WS Phase 24, notifications Phase 12+).
 */
export function TopBar() {
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

      <Button
        variant="outline"
        size="sm"
        className="max-w-44 justify-between gap-2 md:min-w-44"
      >
        <span className="truncate text-muted-foreground">
          No project selected
        </span>
        <ChevronsUpDown
          aria-hidden
          className="size-3.5 text-muted-foreground"
        />
      </Button>

      <Button
        variant="outline"
        size="sm"
        aria-label="Search (⌘K)"
        className="ml-auto justify-start gap-2 text-muted-foreground md:w-56"
      >
        <Search aria-hidden className="size-3.5" />
        <span className="hidden md:inline">Search…</span>
        <kbd className="ml-auto hidden rounded border bg-muted px-1.5 font-mono text-[10px] md:inline">
          ⌘K
        </kbd>
      </Button>

      <ConnectionPill />

      <Bell />

      <ThemeToggle />

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
