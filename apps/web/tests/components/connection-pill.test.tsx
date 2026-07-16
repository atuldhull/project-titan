import { describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { ConnectionPill } from "@/components/shell/connection-pill";
import { useConnectionStore } from "@/lib/ws/connection-store";

/**
 * TITAN-FRT-020: the pill is always visible and reflects the socket state.
 * Also guards the zustand-v5 snapshot rule — an object-returning selector here
 * would make useSyncExternalStore loop instead of rendering.
 */
describe("ConnectionPill", () => {
  it("renders each connection state", () => {
    const cases: [string, string][] = [
      ["live", "Live"],
      ["reconnecting", "Reconnecting…"],
      ["offline", "Offline"],
      ["connecting", "Connecting…"],
    ];
    for (const [status, label] of cases) {
      useConnectionStore.setState({
        status: status as "live",
        degraded: status === "offline",
      });
      const { unmount } = render(<ConnectionPill />);
      expect(screen.getByRole("status")).toHaveTextContent(label);
      unmount();
    }
  });

  it("goes live → reconnecting → live in place", () => {
    useConnectionStore.setState({ status: "live", degraded: false });
    render(<ConnectionPill />);
    expect(screen.getByRole("status")).toHaveTextContent("Live");

    act(() => {
      useConnectionStore.setState({ status: "reconnecting", degraded: false });
    });
    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting…");

    act(() => {
      useConnectionStore.setState({ status: "live", degraded: false });
    });
    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });
});
