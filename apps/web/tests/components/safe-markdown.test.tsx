import { render } from "@testing-library/react";
import Markdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import { describe, expect, it } from "vitest";

import { SafeMarkdown } from "@/components/ui/safe-markdown";

/**
 * TITAN-SEC-072 acceptance: agent-derived Markdown renders INERT, and the
 * rehype-sanitize allowlist is proven LOAD-BEARING (not vacuous defense-in-depth)
 * by contrasting a raw-HTML pipeline with and without it.
 */
const RAW_HTML_XSS = [
  '<img src=x onerror="alert(1)">',
  "<script>alert('xss')</script>",
  '<iframe src="javascript:alert(1)"></iframe>',
  "<svg><script>alert(1)</script></svg>",
  '<div onmouseover="alert(1)">hover</div>',
  '<a href="javascript:alert(1)">x</a>',
];
const MARKDOWN_XSS = [
  "[click me](javascript:alert(document.cookie))",
  "![x](javascript:alert(1))",
];

/** The executable/exfiltration primitives that must never reach the DOM. */
function dangerous(container: HTMLElement) {
  const jsUrls = [...container.querySelectorAll("a, img")].filter((el) =>
    (el.getAttribute("href") ?? el.getAttribute("src") ?? "")
      .replace(/\s/g, "")
      .toLowerCase()
      .startsWith("javascript:"),
  ).length;
  return {
    scripts: container.querySelectorAll("script").length,
    iframes: container.querySelectorAll("iframe").length,
    onHandlers: container.querySelectorAll("[onerror], [onmouseover]").length,
    jsUrls,
  };
}

const CLEAN = { scripts: 0, iframes: 0, onHandlers: 0, jsUrls: 0 };

describe("SafeMarkdown (TITAN-SEC-072)", () => {
  it.each([...RAW_HTML_XSS, ...MARKDOWN_XSS])("renders agent payload inert: %s", (payload) => {
    const { container } = render(<SafeMarkdown>{payload}</SafeMarkdown>);
    expect(dangerous(container)).toEqual(CLEAN);
  });

  it("still renders ordinary agent markdown, incl. SAFE links and images", () => {
    const { container } = render(
      <SafeMarkdown>
        {"**bold**, _em_, [link](https://ex.com), and ![a](https://h/x.png)."}
      </SafeMarkdown>,
    );
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("a")?.getAttribute("href")).toBe("https://ex.com");
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://h/x.png");
    expect(dangerous(container)).toEqual(CLEAN); // safe content is not flagged
  });
});

/**
 * The load-bearing proof that rehype-sanitize is a REAL control (not vacuous
 * defense-in-depth): remove it from the `[rehypeRaw, rehypeSanitize]` pipeline
 * below and the second test goes RED (the first proves the corpus materialises
 * live vectors once raw HTML is parsed).
 *
 * RESIDUAL (honest): Suite 1 renders the real SafeMarkdown, which has NO
 * `rehype-raw`, so react-markdown 9 already ignores raw HTML and strips
 * `javascript:` URLs — meaning Suite 1 stays green even if `rehypeSanitize` were
 * deleted from the component. That gap is unavoidable (a CommonMark-only pipeline
 * cannot exercise the sanitiser), so the PRIMARY regression guards for the
 * component are the ESLint `dangerouslySetInnerHTML` ban + the no-`rehype-raw`
 * invariant; rehype-sanitize is the belt-and-suspenders whose FUNCTION this suite
 * proves, so it bites the moment anyone adds `rehype-raw`.
 */
describe("rehype-sanitize is load-bearing (not vacuous)", () => {
  it("parsed raw HTML is genuinely DANGEROUS without the sanitiser", () => {
    const total = RAW_HTML_XSS.reduce((n, payload) => {
      const { container } = render(<Markdown rehypePlugins={[rehypeRaw]}>{payload}</Markdown>);
      const d = dangerous(container);
      return n + d.scripts + d.iframes + d.jsUrls;
    }, 0);
    expect(total).toBeGreaterThan(0); // the corpus really does materialise live vectors
  });

  it("the SAME parsed raw HTML is INERT once rehype-sanitize runs", () => {
    for (const payload of RAW_HTML_XSS) {
      const { container } = render(
        <Markdown rehypePlugins={[rehypeRaw, rehypeSanitize]}>{payload}</Markdown>,
      );
      expect(dangerous(container)).toEqual(CLEAN);
    }
  });
});
