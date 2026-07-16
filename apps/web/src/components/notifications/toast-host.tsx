"use client";

import { useEffect, useState } from "react";
import { Toaster } from "sonner";

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

export function ToastHost() {
  useNotificationBroadcastSync();
  const theme = useDocumentTheme();
  return (
    <Toaster
      position="bottom-right"
      visibleToasts={TOAST_MAX_VISIBLE}
      duration={TOAST_AUTODISMISS_MS}
      closeButton
      theme={theme}
    />
  );
}
