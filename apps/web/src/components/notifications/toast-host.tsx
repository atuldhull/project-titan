"use client";

import { useEffect, useState } from "react";
import { Toaster } from "sonner";

import { useAnnounceStore } from "@/lib/notifications/announce";
import { useNotificationBroadcastSync } from "@/lib/notifications/hooks";
import { TOAST_AUTODISMISS_MS, TOAST_MAX_VISIBLE } from "@/stores/toasts";

/**
 * Mounts the sonner toast surface (Vol 4 §7.1): bottom-right, at most 3 stacked,
 * 6 s auto-dismiss (criticals persist via Infinity duration set per-toast). Also
 * activates cross-tab unread sync (TITAN-FRT-038). Theme-aware: it tracks the
 * `.dark` class the theme toggle stamps on `<html>`.
 */
function useDocumentTheme(): "dark" | "light" {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  useEffect(() => {
    const read = () =>
      setTheme(
        document.documentElement.classList.contains("dark") ? "dark" : "light",
      );
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, []);
  return theme;
}

/**
 * The assertive half of TITAN-FRT-043 ("toasts polite, critical alerts
 * assertive"). sonner wraps its stack in a hardcoded `aria-live="polite"`
 * container and 1.7.4 exposes no way to change that, so criticals get their own
 * region here; see lib/notifications/announce.ts for the full reasoning.
 *
 * It is mounted BESIDE the Toaster rather than inside it: sonner owns and
 * re-renders its subtree, and a live region it can unmount is a live region
 * that sometimes doesn't fire.
 */
function CriticalAnnouncer() {
  const latest = useAnnounceStore((state) => state.latest);
  return (
    <div role="alert" aria-live="assertive" aria-atomic className="sr-only">
      {/* Keyed on `seq`, not on the text: an identical repeat alert must still
          mutate the DOM to be spoken (see announce.ts). */}
      {latest ? <span key={latest.seq}>{latest.text}</span> : null}
    </div>
  );
}

export function ToastHost() {
  useNotificationBroadcastSync();
  const theme = useDocumentTheme();
  return (
    <>
      <Toaster
        position="bottom-right"
        visibleToasts={TOAST_MAX_VISIBLE}
        duration={TOAST_AUTODISMISS_MS}
        closeButton
        theme={theme}
        /*
         * Clear the < 768 bottom tab bar (Vol 4 §8).
         *
         * Below 600px sonner pins its stack to `bottom: 16px` at full width
         * with `z-index: 999999999`, so a toast paints OVER the fixed tab bar
         * and swallows taps on Org/Board/Agents/Convos/More. A z-index fight is
         * unwinnable at that magnitude, and raising the bar above the toast
         * would just invert the problem — the toast's own close button would go
         * under the bar. Offsetting leaves both reachable: the bar is 56px, so
         * 4.5rem clears it with sonner's usual 16px gap intact.
         *
         * `mobileOffset` is sonner 1.7.4's <600px value; desktop placement,
         * where there is no tab bar at all, is untouched.
         */
        mobileOffset={{ bottom: "4.5rem", left: "1rem", right: "1rem" }}
      />
      <CriticalAnnouncer />
    </>
  );
}
