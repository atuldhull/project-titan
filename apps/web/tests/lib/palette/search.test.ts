/**
 * Command palette ranking (TITAN-FRT-007).
 *
 * The requirement names one concrete case — typing "kan net" surfaces the
 * netflix-clone board — so that case is the first test, built from the REAL
 * item the palette generates rather than a convenient string.
 */
import { describe, expect, it } from "vitest";

import { buildItems } from "@/lib/palette/items";
import { rank, scoreQuery, scoreTerm } from "@/lib/palette/search";
import type { AgentOut, ProjectOut, TaskOut } from "@titan/api-client";

const project = (over: Partial<ProjectOut> = {}): ProjectOut => ({
  id: "p1",
  name: "netflix-clone",
  display_name: "Netflix Clone",
  brief: "streaming",
  status: "active",
  mode: "supervised",
  llm_profile: "balanced",
  version: 1,
  created_at: "2026-07-01T00:00:00Z",
  created_by: "u1",
  ...over,
});

describe("the acceptance criterion: 'kan net' finds the netflix-clone board", () => {
  const items = buildItems({
    projects: [
      project(),
      project({
        id: "p2",
        name: "spotify-clone",
        display_name: "Spotify Clone",
      }),
    ],
    agents: [],
    tasks: [],
  });

  it("ranks that board first", () => {
    const results = rank("kan net", items);
    expect(results[0]?.href).toBe("/projects/p1/board");
  });

  it("matches on the SYNONYM, not just the visible title", () => {
    // The screen is called "Board"; "kan" only matches because the item carries
    // "kanban" as a keyword. Without that this criterion is unmeetable.
    expect(rank("kan", items).map((item) => item.href)).toContain(
      "/projects/p1/board",
    );
  });

  it("requires EVERY term to match — 'kan spot' must not return netflix", () => {
    const hrefs = rank("kan spot", items).map((item) => item.href);
    expect(hrefs).toContain("/projects/p2/board");
    expect(hrefs).not.toContain("/projects/p1/board");
  });

  it("generates project-scoped screens for EVERY project, not just an active one", () => {
    // Two terms matching two different words of one item is only possible if the
    // item exists at all. Generating board items only for the project currently
    // in the URL would make "kan net" unmatchable from anywhere else.
    const boards = items.filter((item) => item.href.endsWith("/board"));
    expect(boards).toHaveLength(2);
  });
});

describe("scoreTerm", () => {
  it("returns null when the term is not a subsequence at all", () => {
    expect(scoreTerm("zzz", "Netflix Clone")).toBeNull();
    expect(scoreTerm("toolong", "short")).toBeNull();
  });

  it("scores a word-start match above a mid-word one", () => {
    const start = scoreTerm("clone", "Netflix Clone")!;
    const middle = scoreTerm("lone", "Netflix Clone")!;
    expect(start).toBeGreaterThan(middle);
  });

  it("scores a contiguous match above a scattered one", () => {
    const contiguous = scoreTerm("net", "netflix")!;
    const scattered = scoreTerm("net", "nice easy team")!;
    expect(contiguous).toBeGreaterThan(scattered);
  });

  it("is case-insensitive in both directions", () => {
    expect(scoreTerm("NET", "netflix")).toBe(scoreTerm("net", "NETFLIX"));
  });

  it("treats a hyphen as a word boundary", () => {
    // "netflix-clone" must let `clone` score as a word start, or the natural
    // way to type a project slug ranks below noise.
    expect(scoreTerm("clone", "netflix-clone")!).toBeGreaterThan(
      scoreTerm("lone", "netflix-clone")!,
    );
  });

  it("matches an empty term", () => {
    expect(scoreTerm("", "anything")).toBe(0);
  });

  it("survives a character whose lowercase form is LONGER than itself", () => {
    // U+0130 (İ, Turkish dotted capital I) lowercases to TWO code units,
    // "i" + U+0307. The DP indexes the ORIGINAL text with positions derived
    // from the lowercased copy, so one of these anywhere in a project, agent or
    // task name shifted every later index and then read past the end of the
    // string — throwing inside the palette's render, which took down the whole
    // dashboard shell for anyone with a Turkish name in their data.
    expect("İ".toLowerCase().length).toBe(2); // the premise, pinned
    expect(() => scoreTerm("stanbul", "İstanbul App")).not.toThrow();
    expect(scoreTerm("stanbul", "İstanbul App")).not.toBeNull();
    expect(() => scoreTerm("app", "İİİİ App")).not.toThrow();
    expect(scoreTerm("app", "İİİİ App")).not.toBeNull();
  });

  it("keeps ranking correct AFTER such a character", () => {
    // The crash is the loud symptom; silent index drift is the quiet one. A
    // word-start match must still outscore a mid-word one when an İ precedes it.
    const start = scoreTerm("app", "İstanbul App")!;
    const middle = scoreTerm("pp", "İstanbul App")!;
    expect(start).toBeGreaterThan(middle);
  });
});

describe("scoreQuery", () => {
  it("sums the terms", () => {
    const item = { haystack: "Board netflix-clone" };
    const both = scoreQuery("board netflix", item)!;
    expect(both).toBe(
      scoreTerm("board", item.haystack)! + scoreTerm("netflix", item.haystack)!,
    );
  });

  it("is null when any single term fails", () => {
    expect(scoreQuery("board zzz", { haystack: "Board netflix" })).toBeNull();
  });

  it("treats an all-whitespace query as no query", () => {
    expect(scoreQuery("   ", { haystack: "x" })).toBe(0);
  });
});

describe("rank", () => {
  const items = [
    { haystack: "alpha", id: "a" },
    { haystack: "alpine", id: "b" },
    { haystack: "beta", id: "c" },
  ];

  it("keeps the caller's order for an empty query", () => {
    // The palette's opening state is a curated list of screens; re-sorting it by
    // an arbitrary score would make ⌘K show a different thing every time.
    expect(rank("", items).map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("drops non-matches", () => {
    expect(rank("alp", items).map((i) => i.id)).toEqual(["a", "b"]);
  });

  it("breaks ties on the caller's order, so the list is stable", () => {
    // An unstable sort moves the highlighted row out from under the user's
    // Enter between renders.
    const tied = [
      { haystack: "same", id: "first" },
      { haystack: "same", id: "second" },
    ];
    expect(rank("same", tied).map((i) => i.id)).toEqual(["first", "second"]);
  });

  it("does not mutate the input", () => {
    const original = [...items];
    rank("", items);
    expect(items).toEqual(original);
  });
});

describe("buildItems", () => {
  it("includes agents and tasks alongside screens and projects (TITAN-FRT-007)", () => {
    const agent: AgentOut = {
      id: "a1",
      name: "Backend Engineer",
      role: "backend",
      roster_id: "eng_backend",
      status: "idle",
      project_id: "p1",
      current_task_id: null,
      config: {},
      model_binding: {},
      version: 1,
      created_at: "2026-07-01T00:00:00Z",
    };
    const task: TaskOut = {
      id: "t1",
      title: "Add the watch list",
      project_id: "p1",
      status: "in_progress",
      spec: "",
      priority: 2,
      version: 1,
      created_at: "2026-07-01T00:00:00Z",
      assignee_agent_id: null,
      completed_at: null,
      created_by_agent_id: null,
      estimate_points: null,
      milestone_id: null,
      parent_id: null,
      reviewer_agent_id: null,
      sprint_id: null,
      wfl_lane: "build",
    };
    const items = buildItems({
      projects: [project()],
      agents: [agent],
      tasks: [{ task, project: project() }],
    });
    const groups = new Set(items.map((item) => item.group));
    expect(groups).toEqual(new Set(["Screens", "Projects", "Agents", "Tasks"]));

    expect(rank("watch", items)[0]?.href).toBe("/projects/p1/board?task=t1");
    expect(rank("backend", items)[0]?.href).toBe("/agents/a1");
  });

  it("gives every item a unique id", () => {
    // Duplicate ids are a React key collision, and the palette renders a list.
    const items = buildItems({
      projects: [project(), project({ id: "p2", name: "other" })],
      agents: [],
      tasks: [],
    });
    expect(new Set(items.map((item) => item.id)).size).toBe(items.length);
  });
});
