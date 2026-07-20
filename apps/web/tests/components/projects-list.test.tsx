import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { usePrimarySearchFocus } from "@/lib/shortcuts/primary-search";

/**
 * TITAN-FRT-043 ("tables as tables") + §8 ("< 768: tables → card lists") for the
 * Projects list.
 *
 * READ THIS BEFORE TRUSTING THESE ASSERTIONS: jsdom does not evaluate Tailwind,
 * so `hidden md:block` and `md:hidden` do NOTHING here — BOTH the table and the
 * card list are in the DOM at once, which is never true in a browser. Every
 * query below is therefore scoped to one branch or the other. A bare
 * `getByRole("link", { name: "netflix-clone" })` would match twice and throw,
 * and a bare `getAllByRole(...)` would silently count both branches and pass no
 * matter what either one rendered.
 */

const PROJECTS = [
  {
    id: "p1",
    name: "netflix-clone",
    display_name: "netflix-clone",
    status: "intake",
    created_at: "2026-07-19T10:00:00Z",
  },
  {
    id: "p2",
    name: "banking-sys",
    display_name: "banking-sys",
    status: "released",
    created_at: "2026-07-18T10:00:00Z",
  },
];

vi.mock("@/lib/projects/hooks", () => ({
  useProjects: () => ({
    data: { items: PROJECTS },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useAgents: () => ({ data: { items: [] } }),
  useProjectBoards: () => [
    { data: undefined, isLoading: false, isError: false },
    { data: undefined, isLoading: false, isError: false },
  ],
}));

const { ProjectsList } = await import("@/components/projects/projects-list");

/** The md+ branch: a real <table>. */
function table(): HTMLElement {
  return screen.getByRole("table");
}

/** The sub-md branch: the card <ul>, which is the table's sibling. */
function cardList(container: HTMLElement): HTMLElement {
  const list = container.querySelector("ul.md\\:hidden");
  if (!list) {
    throw new Error("card list not rendered");
  }
  return list as HTMLElement;
}

describe("ProjectsList semantics", () => {
  it("renders a real table with a header row at md+", () => {
    render(<ProjectsList />);
    const headers = within(table())
      .getAllByRole("columnheader")
      .map((h) => h.textContent);

    // The defect this replaced: a <ul> of flex rows with NO headers anywhere,
    // so "0 agents" / "CI –" / "7/19/2026" reached a screen reader as a flat
    // run of text with nothing to say which column any of it belonged to.
    expect(headers).toEqual([
      "Project",
      "Status",
      "Progress",
      "Agents",
      "CI",
      "Created",
    ]);
  });

  it("makes the project name the row header the other cells are read against", () => {
    render(<ProjectsList />);
    const rowHeaders = within(table())
      .getAllByRole("rowheader")
      .map((h) => h.textContent);
    expect(rowHeaders).toEqual(["netflix-clone", "banking-sys"]);
  });

  it("puts the link inside the row-header cell, not on the <tr>", () => {
    // An <a> cannot wrap <td>s, and an onClick on the <tr> is a target no
    // keyboard or screen reader can reach.
    render(<ProjectsList />);
    const link = within(table()).getByRole("link", { name: "netflix-clone" });
    expect(link).toHaveAttribute("href", "/projects/p1");
    expect(link.closest("th")).not.toBeNull();
  });

  it("renders the SAME projects as cards below md", () => {
    const { container } = render(<ProjectsList />);
    const cards = within(cardList(container)).getAllByRole("link");

    expect(cards.map((a) => a.getAttribute("href"))).toEqual([
      "/projects/p1",
      "/projects/p2",
    ]);
  });

  it("registers the filter input as the `/` target and actually focuses it", () => {
    // §10: `/` focuses the screen's primary search. Two things can silently
    // fail here and neither shows up in a render assertion: the screen might
    // never register (so `/` falls through to the palette), or the ref might
    // not reach the real <input> — `Input` is a plain function component, so
    // this also pins that React 19 ref-as-prop forwarding keeps working.
    let focus: (() => void) | null = null;
    function Probe() {
      focus = usePrimarySearchFocus();
      return null;
    }
    render(
      <>
        <ProjectsList />
        <Probe />
      </>,
    );

    const input = screen.getByLabelText("Filter projects by name");
    expect(input).not.toHaveFocus();

    expect(focus).toBeTypeOf("function");
    act(() => focus?.());
    expect(input).toHaveFocus();
  });

  it("labels each card value inline — cards have no column headers to inherit", () => {
    const { container } = render(<ProjectsList />);
    const first = within(cardList(container)).getAllByRole("listitem")[0];

    // The same information the <th>s supply at md+. Without these the card
    // branch is the exact accessibility defect the table branch just fixed.
    expect(first).toHaveTextContent("0 agents");
    expect(first).toHaveTextContent("CI –");
    expect(first).toHaveTextContent(/Created/);
  });
});
