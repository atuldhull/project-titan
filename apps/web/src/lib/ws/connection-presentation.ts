import type { ConnectionStatus } from "./connection-store";

/**
 * How a `ConnectionStatus` is SHOWN — the one place the four display strings
 * and the four dot colours are written down.
 *
 * This lives in lib/ws rather than inside the top-bar pill because it has more
 * than one consumer: the pill (components/shell/connection-pill.tsx) and the
 * Overview activity feed both have to say whether the socket is up, and they
 * must not say it differently. When this map was private to the pill the feed
 * had no way to reach it and fell back to rendering the raw status token
 * ("reconnecting"), so the same state was spoken two different ways depending
 * on where you were looking — and the feed's dot could only distinguish live
 * from not-live rather than the four-colour scale.
 *
 * `label` is what a screen reader reads and what is drawn on screen (§9 /
 * TITAN-FRT-044: status is never colour-only, so the text is mandatory, not
 * decorative). `dot` (a filled swatch) and `icon` (a glyph) are the same scale
 * expressed as background and foreground, because the pill draws a dot and the
 * feed draws a Radio icon; both are semantic tokens only, never raw colours
 * (TITAN-FRT-010). `pulse` is suppressed globally under prefers-reduced-motion
 * by the blanket rule in globals.css (TITAN-FRT-045), which is safe precisely
 * because the meaning is carried by `label`.
 */
export const CONNECTION_PRESENTATION: Record<
  ConnectionStatus,
  { label: string; dot: string; icon: string; pulse: boolean }
> = {
  connecting: {
    label: "Connecting…",
    dot: "bg-muted-foreground",
    icon: "text-muted-foreground",
    pulse: true,
  },
  live: {
    label: "Live",
    dot: "bg-success",
    icon: "text-success",
    pulse: false,
  },
  reconnecting: {
    label: "Reconnecting…",
    dot: "bg-warning",
    icon: "text-warning",
    pulse: true,
  },
  offline: {
    label: "Offline",
    dot: "bg-destructive",
    icon: "text-destructive",
    pulse: false,
  },
};
