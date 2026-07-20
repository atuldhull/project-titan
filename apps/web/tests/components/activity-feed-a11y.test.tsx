import { describe, expect, it } from "vitest";
import { act, render, screen, within } from "@testing-library/react";

import { ActivityFeed } from "@/components/overview/activity-feed";
import { CONNECTION_PRESENTATION } from "@/lib/ws/connection-presentation";
import { useConnectionStore } from "@/lib/ws/connection-store";

/**
 * TITAN-FRT-043 / 044 for the Overview activity feed.
 *
 * The `rows` prop is the injection seam — supplying it short-circuits the WS
 * subscription, so these assertions are about STRUCTURE, not about traffic.
 */
const ROWS = [
  {
    id: "a",
    ts: "2026-07-19T12:04:00Z",
    type: "task.created",
    projectId: "p1",
  },
  {
    id: "b",
    ts: "2026-07-19T12:03:00Z",
    type: "run.finished",
    projectId: null,
  },
];

describe("ActivityFeed accessibility", () => {
  it("exposes the scroller as a named log, not as a live region", () => {
    render(<ActivityFeed rows={ROWS} />);
    const log = screen.getByRole("log");

    // Named from the panel heading so a screen reader announces WHICH log.
    expect(log).toHaveAccessibleName("Live activity");

    // The critical negative: the list is virtualized, so a live region here
    // would re-announce recycled rows on every scroll and re-measure.
    //
    // Asserting the ABSENCE of aria-live is not enough and was the original
    // bug in this test: `role="log"` carries an implicit `aria-live="polite"`,
    // so a scroller with no aria-live attribute at all is still a live region.
    // Only an explicit "off" overrides a role's implicit value.
    expect(log).toHaveAttribute("aria-live", "off");
  });

  it("has a separate polite region that is NOT the virtualized list", () => {
    const { container } = render(<ActivityFeed rows={ROWS} />);
    const polite = container.querySelector('[aria-live="polite"]');

    expect(polite).not.toBeNull();
    expect(polite).toHaveClass("sr-only");
    // It must not CONTAIN the log, or it inherits the recycling problem the
    // separation exists to avoid.
    expect(within(polite as HTMLElement).queryByRole("log")).toBeNull();
  });

  it("reports the real connection status in text, not colour alone", () => {
    // TITAN-FRT-044. The dot used to be a hardcoded `text-success` that stayed
    // green while the socket was down — it asserted something false, and colour
    // was the only channel.
    //
    // The text is asserted through CONNECTION_PRESENTATION rather than against
    // literals, because the point of that map is that the feed and the top-bar
    // pill word the same state identically; hardcoding "Live" here would pass
    // just as happily if the feed drifted back to its own vocabulary.
    // getByText matches an element's WHOLE normalized text, so "Live" hits the
    // status label and not the "Live activity" heading it sits beside — which
    // is what makes the negative assertion below meaningful rather than
    // accidentally satisfied by a substring.
    useConnectionStore.setState({ status: "live", degraded: false });
    render(<ActivityFeed rows={ROWS} />);
    expect(screen.getByText(CONNECTION_PRESENTATION.live.label)).toBeVisible();

    act(() => {
      useConnectionStore.setState({ status: "offline", degraded: true });
    });
    expect(
      screen.getByText(CONNECTION_PRESENTATION.offline.label),
    ).toBeVisible();
    expect(screen.queryByText(CONNECTION_PRESENTATION.live.label)).toBeNull();
  });

  it("does not announce the connection status — the top-bar pill owns that", () => {
    // <ConnectionPill> is mounted on every screen and is already a
    // role="status" live region for this exact value. A second one inside the
    // feed makes a screen reader read every reconnect twice, which is why the
    // feed's copy is silent text. The only live region the feed may own is its
    // own arrival announcer.
    useConnectionStore.setState({ status: "reconnecting", degraded: false });
    render(<ActivityFeed rows={ROWS} />);

    expect(screen.queryByRole("status")).toBeNull();
    // …but it is still SHOWN — silencing it must not mean deleting it, or the
    // dot goes back to being the only channel (§9).
    expect(
      screen.getByText(CONNECTION_PRESENTATION.reconnecting.label),
    ).toBeVisible();
  });
});
