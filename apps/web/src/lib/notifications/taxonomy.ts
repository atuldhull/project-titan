/**
 * The Vol 4 §7.1 notification taxonomy — the 5 categories and the default
 * channels each routes to. This table is normative: `approval` and `security`
 * are the human-action categories that reach the Attention Queue.
 */

export type NotificationCategory =
  "approval" | "failure" | "progress" | "activity" | "security";

export type NotificationSeverity = "critical" | "high" | "info" | "low";

export interface ChannelRouting {
  /** Bottom-right sonner toast. */
  readonly toast: boolean;
  /** Bell + Notification Center history. */
  readonly bell: boolean;
  /** Overview Attention Queue — requires human action, never auto-dismissed. */
  readonly attentionQueue: boolean;
  /** Activity feed only (muted by default). */
  readonly feedOnly: boolean;
}

interface CategoryDefault {
  readonly severity: NotificationSeverity;
  readonly channels: ChannelRouting;
}

export const CATEGORY_DEFAULTS: Record<NotificationCategory, CategoryDefault> =
  {
    approval: {
      severity: "critical",
      channels: {
        toast: true,
        bell: true,
        attentionQueue: true,
        feedOnly: false,
      },
    },
    failure: {
      severity: "high",
      channels: {
        toast: true,
        bell: true,
        attentionQueue: false,
        feedOnly: false,
      },
    },
    progress: {
      severity: "info",
      channels: {
        toast: false,
        bell: true,
        attentionQueue: false,
        feedOnly: false,
      },
    },
    activity: {
      severity: "low",
      channels: {
        toast: false,
        bell: false,
        attentionQueue: false,
        feedOnly: true,
      },
    },
    security: {
      severity: "critical",
      channels: {
        toast: true,
        bell: true,
        attentionQueue: true,
        feedOnly: false,
      },
    },
  };

const KNOWN: ReadonlySet<string> = new Set(Object.keys(CATEGORY_DEFAULTS));

export function isCategory(value: string): value is NotificationCategory {
  return KNOWN.has(value);
}

/**
 * Channel routing for a notification.
 *
 * The §7.1 table is keyed by category, but the backend's real emitters do NOT
 * speak that vocabulary — `handoffs.service` sends `category="pipeline"` and
 * `plugins.supervisor` sends `category="plugin"`, both with
 * `severity="critical"`. A category-only lookup therefore dropped genuine
 * human-action alerts ("pipeline needs human input", "plugin quarantined") into
 * a silent bell row. So an unknown category falls back to routing by SEVERITY:
 * a critical is treated like `approval` (toast + bell + attention queue), a high
 * like `failure`, and anything else stays bell-only. Known categories keep the
 * exact §7.1 routing.
 */
export function routeNotification(
  category: string,
  severity?: string,
): ChannelRouting {
  if (isCategory(category)) {
    return CATEGORY_DEFAULTS[category].channels;
  }
  if (severity === "critical") {
    return CATEGORY_DEFAULTS.approval.channels;
  }
  if (severity === "high") {
    return CATEGORY_DEFAULTS.failure.channels;
  }
  return { toast: false, bell: true, attentionQueue: false, feedOnly: false };
}

export function isCritical(severity: string): boolean {
  return severity === "critical";
}

/** Severity rank for the "never displaced by lower-severity" rule (higher wins). */
export function severityRank(severity: string): number {
  switch (severity) {
    case "critical":
      return 3;
    case "high":
      return 2;
    case "info":
      return 1;
    default:
      return 0;
  }
}
