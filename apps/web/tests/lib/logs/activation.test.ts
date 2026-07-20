/**
 * §10's `Space` binding on Logs must not eat a focused button's activation key.
 *
 * The engine `preventDefault()`s before it runs a shortcut, so a bare `Space`
 * registration silently kills Export / Wrap / Tailing / the level chips / each
 * gap's [load] for keyboard users (TITAN-FRT-042). These pin the three outcomes.
 */

import { describe, expect, it } from "vitest";

import { spaceOutcome } from "@/lib/logs/activation";

function el(html: string): Element {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host.firstElementChild as Element;
}

describe("spaceOutcome", () => {
  it("toggles the tail when nothing interesting has focus", () => {
    expect(spaceOutcome(null)).toBe("toggle-tail");
    expect(spaceOutcome(el("<div>log body</div>"))).toBe("toggle-tail");
  });

  it("replays the click for a focused button rather than pausing the tail", () => {
    expect(spaceOutcome(el("<button>Export</button>"))).toBe("activate");
  });

  it("finds the control when focus reports an inner node", () => {
    // The real case: `<button><svg/>Export</button>`. A tagName test on the
    // reported element misses it and the button goes dead.
    const button = el("<button><span id='inner'>Export</span></button>");
    const inner = button.querySelector("#inner");
    expect(spaceOutcome(inner)).toBe("activate");
  });

  it("treats ARIA widgets as activation targets too", () => {
    expect(spaceOutcome(el("<div role='button'>x</div>"))).toBe("activate");
    expect(spaceOutcome(el("<div role='switch'>x</div>"))).toBe("activate");
    expect(spaceOutcome(el("<div role='tab'>x</div>"))).toBe("activate");
  });

  it("ignores a focused link — Space scrolls a link, it does not follow it", () => {
    // `activate` here would invent a navigation on a key that never navigates.
    expect(spaceOutcome(el("<a href='/logs'>logs</a>"))).toBe("ignore");
  });

  it("does not treat an anchor without href as focusable chrome", () => {
    expect(spaceOutcome(el("<a>not a link</a>"))).toBe("toggle-tail");
  });
});
