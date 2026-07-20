/**
 * The shortcut engine's rules (Vol 4 §10, TITAN-FRT-048).
 *
 * All of §10's hard parts are edge cases — what counts as a text field, when a
 * sequence expires, which chords we may claim — so they are pinned here rather
 * than in a browser, where each one would cost a page load to observe.
 */
import { describe, expect, it } from "vitest";

import {
  isSequenceLive,
  isTextEntry,
  NO_SEQUENCE,
  SEQUENCE_TIMEOUT_MS,
  tokenFor,
} from "@/lib/shortcuts/keys";
import {
  byGroup,
  caps,
  resolve,
  validate,
  type Shortcut,
} from "@/lib/shortcuts/registry";

const shortcut = (
  over: Partial<Shortcut> & Pick<Shortcut, "id" | "keys">,
): Shortcut => ({
  label: over.id,
  group: "Global",
  run: () => {},
  ...over,
});

/** A KeyboardEvent standing in for a real press, with a chosen target. */
function press(
  key: string,
  options: {
    meta?: boolean;
    ctrl?: boolean;
    alt?: boolean;
    repeat?: boolean;
    target?: EventTarget | null;
  } = {},
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key,
    metaKey: options.meta,
    ctrlKey: options.ctrl,
    altKey: options.alt,
    repeat: options.repeat,
  });
  if (options.target !== undefined) {
    Object.defineProperty(event, "target", { value: options.target });
  }
  return event;
}

describe("tokenFor", () => {
  it("collapses Meta and Control into one Mod token", () => {
    // A binding is written once and works on both platforms; without this the
    // whole table would need duplicating.
    expect(tokenFor(press("k", { meta: true }))).toBe("Mod+k");
    expect(tokenFor(press("k", { ctrl: true }))).toBe("Mod+k");
  });

  it("lower-cases a printable key so G and g are one binding", () => {
    expect(tokenFor(press("G"))).toBe("g");
  });

  it("keeps ? as itself rather than treating Shift as a modifier", () => {
    // `?` IS Shift+/ on a US layout and something else elsewhere. Binding the
    // printed character is the only thing that survives a non-US keyboard.
    expect(tokenFor(press("?"))).toBe("?");
  });

  it("names the space bar", () => {
    expect(tokenFor(press(" "))).toBe("Space");
  });

  it("keeps named keys intact and composes Alt", () => {
    expect(tokenFor(press("ArrowUp", { alt: true }))).toBe("Alt+ArrowUp");
    expect(tokenFor(press("Escape"))).toBe("Escape");
  });
});

describe("isTextEntry", () => {
  const make = (html: string): Element => {
    const host = document.createElement("div");
    host.innerHTML = html;
    return host.firstElementChild as Element;
  };

  it("is true for the fields typing belongs in", () => {
    expect(isTextEntry(make("<input />"))).toBe(true);
    expect(isTextEntry(make("<textarea></textarea>"))).toBe(true);
    expect(isTextEntry(make('<div contenteditable="true">x</div>'))).toBe(true);
    expect(isTextEntry(make('<div role="textbox">x</div>'))).toBe(true);
  });

  it("is true for a Radix combobox trigger, which the old tag checks missed", () => {
    // The three hand-rolled listeners this engine replaced each tested
    // `tagName === "INPUT"`, so `/` fired while a select was open.
    expect(isTextEntry(make('<div role="combobox"></div>'))).toBe(true);
  });

  it("is true inside a widget with its own type-ahead", () => {
    // An open Select or DropdownMenu is not a text field, but typing in one
    // jumps to a matching item. It is also NOT behind the aria-hidden overlay —
    // it is the part of the page that stays visible — so nothing else catches
    // this, and a bare `g` would both be swallowed and navigate.
    expect(
      isTextEntry(
        make('<div role="listbox"><div>x</div></div>').firstElementChild,
      ),
    ).toBe(true);
    expect(isTextEntry(make('<div role="menu"></div>'))).toBe(true);
    expect(isTextEntry(make('<div role="menuitem"></div>'))).toBe(true);
    expect(isTextEntry(make('<div role="option"></div>'))).toBe(true);
  });

  it("is FALSE for inputs that are not text", () => {
    // Treating these as text entry would make every shortcut dead whenever a
    // checkbox happened to hold focus.
    expect(isTextEntry(make('<input type="checkbox" />'))).toBe(false);
    expect(isTextEntry(make('<input type="radio" />'))).toBe(false);
    expect(isTextEntry(make('<input type="button" />'))).toBe(false);
  });

  it("is false for ordinary elements and for contenteditable=false", () => {
    expect(isTextEntry(make("<button>x</button>"))).toBe(false);
    expect(isTextEntry(make('<div contenteditable="false">x</div>'))).toBe(
      false,
    );
    expect(isTextEntry(null)).toBe(false);
  });

  it("walks up from a descendant of the field", () => {
    const host = document.createElement("div");
    host.innerHTML = '<div contenteditable="true"><span>deep</span></div>';
    expect(isTextEntry(host.querySelector("span"))).toBe(true);
  });
});

describe("isSequenceLive", () => {
  it("is dead at exactly the timeout, not alive", () => {
    // §10 says sequences time out AFTER 1.5 s. An inclusive boundary would make
    // the timeout untestable without sleeping past it.
    const state = { prefix: "g", at: 1000 };
    expect(isSequenceLive(state, 1000 + SEQUENCE_TIMEOUT_MS - 1)).toBe(true);
    expect(isSequenceLive(state, 1000 + SEQUENCE_TIMEOUT_MS)).toBe(false);
  });

  it("is never live with no prefix", () => {
    expect(isSequenceLive(NO_SEQUENCE, 0)).toBe(false);
  });
});

describe("validate", () => {
  it("rejects a chord that would steal a browser accelerator", () => {
    // The rule that actually protects the user is enforced at registration —
    // a reviewer will not catch `Mod+s` being added six phases from now.
    expect(validate(shortcut({ id: "save", keys: "Mod+s" }))).toMatch(
      /not an allowed chord/,
    );
    expect(validate(shortcut({ id: "newtab", keys: "Mod+t" }))).toMatch(
      /not an allowed chord/,
    );
  });

  it("accepts the three chords §10 specifies", () => {
    for (const keys of ["Mod+k", "Mod+p", "Mod+Enter"]) {
      expect(validate(shortcut({ id: keys, keys }))).toBeNull();
    }
  });

  it("rejects a sequence step carrying a modifier", () => {
    expect(validate(shortcut({ id: "x", keys: "g Mod+o" }))).toMatch(
      /may not carry a modifier/,
    );
  });

  it("rejects a three-step sequence", () => {
    expect(validate(shortcut({ id: "x", keys: "g o p" }))).toMatch(
      /at most two steps/,
    );
  });

  it("accepts bare keys, which are not browser accelerators", () => {
    expect(validate(shortcut({ id: "help", keys: "?" }))).toBeNull();
    expect(validate(shortcut({ id: "tail", keys: "Space" }))).toBeNull();
  });
});

describe("resolve", () => {
  const goOrg = shortcut({ id: "goto-o", keys: "g o" });
  const goProjects = shortcut({ id: "goto-p", keys: "g p" });
  const palette = shortcut({ id: "palette", keys: "Mod+k" });
  const search = shortcut({ id: "search", keys: "/" });
  const close = shortcut({
    id: "close",
    keys: "Escape",
    allowInTextEntry: true,
    allowBehindOverlay: true,
  });
  const submit = shortcut({
    id: "submit",
    keys: "Mod+Enter",
    allowInTextEntry: true,
  });
  const all = [goOrg, goProjects, palette, search, close, submit];

  it("arms a prefix, then completes the sequence", () => {
    const armed = resolve(press("g"), all, NO_SEQUENCE, 0);
    expect(armed.kind).toBe("prefix");
    expect(armed.sequence.prefix).toBe("g");

    const done = resolve(press("o"), all, armed.sequence, 100);
    expect(done).toMatchObject({ kind: "run", shortcut: { id: "goto-o" } });
    expect(done.sequence).toEqual(NO_SEQUENCE);
  });

  it("does NOT complete a sequence that has timed out", () => {
    const armed = resolve(press("g"), all, NO_SEQUENCE, 0);
    const late = resolve(
      press("o"),
      all,
      armed.sequence,
      SEQUENCE_TIMEOUT_MS + 1,
    );
    expect(late.kind).toBe("ignore");
  });

  it("cancels on an unmatched second key WITHOUT firing that key's own binding", () => {
    // `g` then `/` must not open search. A half-typed sequence doing something
    // unrelated is worse than doing nothing — the user's intent was `g …`.
    const armed = resolve(press("g"), all, NO_SEQUENCE, 0);
    const stray = resolve(press("/"), all, armed.sequence, 10);
    expect(stray.kind).toBe("ignore");
    expect(stray.sequence).toEqual(NO_SEQUENCE);
  });

  it("does not let a bare modifier press disturb an in-flight sequence", () => {
    // Reaching for a modifier mid-sequence is common; it must not cancel.
    const armed = resolve(press("g"), all, NO_SEQUENCE, 0);
    const held = resolve(press("Shift"), all, armed.sequence, 10);
    expect(held.sequence.prefix).toBe("g");
    const done = resolve(press("p"), all, held.sequence, 20);
    expect(done).toMatchObject({ kind: "run", shortcut: { id: "goto-p" } });
  });

  it("does not arm a prefix from an auto-repeat", () => {
    // Holding `g` would otherwise re-arm forever and swallow every key.
    expect(
      resolve(press("g", { repeat: true }), all, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
  });

  it("suppresses ordinary shortcuts while typing", () => {
    const input = document.createElement("input");
    expect(
      resolve(press("/", { target: input }), all, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
    expect(
      resolve(press("g", { target: input }), all, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
  });

  it("still allows a CHORD while typing, so ⌘K works from a filter box", () => {
    // §10's "disabled inside text inputs" is about printable keys — `g` must be
    // a letter while typing. A Ctrl/⌘ chord cannot be mistaken for typing, and
    // suppressing it would mean ⌘K neither opens the palette from a filter box
    // NOR closes it again, since the palette focuses its own input.
    const input = document.createElement("input");
    expect(
      resolve(press("k", { meta: true, target: input }), all, NO_SEQUENCE, 0),
    ).toMatchObject({ kind: "run", shortcut: { id: "palette" } });
  });

  it("still allows Esc and Mod+Enter while typing — §10's two exceptions", () => {
    const input = document.createElement("input");
    expect(
      resolve(press("Escape", { target: input }), all, NO_SEQUENCE, 0),
    ).toMatchObject({ kind: "run", shortcut: { id: "close" } });
    expect(
      resolve(
        press("Enter", { meta: true, target: input }),
        all,
        NO_SEQUENCE,
        0,
      ),
    ).toMatchObject({ kind: "run", shortcut: { id: "submit" } });
  });

  it("suppresses a screen binding whenever ANY modal is open", () => {
    // The rule that took three screens hand-rolling their own guard before it
    // was obvious. Asking "is the EVENT TARGET hidden" can never work: Radix
    // moves focus into the dialog, which is the one subtree it does not hide.
    // So the engine asks the global question — with a dialog up, `g l` must not
    // navigate the page underneath it.
    const dialog = document.createElement("div");
    dialog.setAttribute("data-slot", "dialog-content");
    dialog.setAttribute("data-state", "open");
    document.body.append(dialog);
    try {
      expect(resolve(press("g"), all, NO_SEQUENCE, 0).kind).toBe("ignore");
      expect(resolve(press("/"), all, NO_SEQUENCE, 0).kind).toBe("ignore");
      // …but the palette must still be able to close itself, and `?` must
      // still reach the help overlay from on top of the palette.
      const opting = [
        shortcut({ id: "palette", keys: "Mod+k", allowBehindOverlay: true }),
      ];
      expect(
        resolve(press("k", { meta: true }), opting, NO_SEQUENCE, 0),
      ).toMatchObject({ kind: "run" });
    } finally {
      dialog.remove();
    }
  });

  it("ignores a CLOSING modal's leftover content", () => {
    // Radix keeps content mounted through the exit animation with
    // data-state="closed"; treating that as open would deaden every shortcut
    // for the length of the animation.
    const dialog = document.createElement("div");
    dialog.setAttribute("data-slot", "dialog-content");
    dialog.setAttribute("data-state", "closed");
    document.body.append(dialog);
    try {
      expect(resolve(press("g"), all, NO_SEQUENCE, 0).kind).toBe("prefix");
    } finally {
      dialog.remove();
    }
  });

  it("leaves Space and Enter to a focused button", () => {
    // Registering §10's bare `Space` for "pause tail" naively makes EVERY
    // button on that screen dead to the keyboard: the engine preventDefaults
    // before dispatching, so tabbing to Export and pressing Space would toggle
    // the log tail instead of pressing Export. Direct TITAN-FRT-042 failure.
    const registered = [shortcut({ id: "tail", keys: "Space", group: "Logs" })];
    const button = document.createElement("button");
    expect(
      resolve(press(" ", { target: button }), registered, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
    // A <span> inside the button is what the browser actually reports.
    const inner = document.createElement("span");
    button.append(inner);
    expect(
      resolve(press(" ", { target: inner }), registered, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
    // With focus on nothing activatable, §10's binding wins as specified.
    expect(
      resolve(
        press(" ", { target: document.body }),
        registered,
        NO_SEQUENCE,
        0,
      ),
    ).toMatchObject({ kind: "run", shortcut: { id: "tail" } });
  });

  it("does NOT treat Space on a link as activation", () => {
    // Space scrolls a link, it does not follow it — so the shortcut is the
    // more useful owner of the key there.
    const registered = [shortcut({ id: "tail", keys: "Space", group: "Logs" })];
    const link = document.createElement("a");
    link.href = "#x";
    expect(
      resolve(press(" ", { target: link }), registered, NO_SEQUENCE, 0),
    ).toMatchObject({ kind: "run" });
  });

  it("suppresses everything except Esc behind an open modal", () => {
    // Radix marks the rest of the page aria-hidden while a dialog is open. A
    // binding firing from under it acts on a screen the user cannot see —
    // Phase 60 shipped exactly that bug on Conversations.
    const host = document.createElement("div");
    host.innerHTML = '<div aria-hidden="true"><button>behind</button></div>';
    const buried = host.querySelector("button");

    expect(
      resolve(
        press("Enter", { meta: true, target: buried }),
        all,
        NO_SEQUENCE,
        0,
      ).kind,
    ).toBe("ignore");
    expect(
      resolve(press("Escape", { target: buried }), all, NO_SEQUENCE, 0),
    ).toMatchObject({ kind: "run", shortcut: { id: "close" } });
  });

  it("matches an allowed chord", () => {
    expect(
      resolve(press("k", { meta: true }), all, NO_SEQUENCE, 0),
    ).toMatchObject({ kind: "run", shortcut: { id: "palette" } });
  });

  it("ignores an unbound chord so the browser keeps it", () => {
    // The caller preventDefaults only on "run"/"prefix", so Ctrl+S must land
    // here or Save stops working.
    expect(resolve(press("s", { meta: true }), all, NO_SEQUENCE, 0).kind).toBe(
      "ignore",
    );
  });

  it("never arms a sequence from a chord", () => {
    const registered = [shortcut({ id: "seq", keys: "g o" })];
    expect(
      resolve(press("g", { meta: true }), registered, NO_SEQUENCE, 0).kind,
    ).toBe("ignore");
  });

  it("prefers an exact single-key match over arming a sequence", () => {
    // A screen binding `g` outright must win over the global `g …` prefix,
    // otherwise registering one would silently disable the other.
    const registered = [
      shortcut({ id: "seq", keys: "g o" }),
      shortcut({ id: "bare", keys: "g" }),
    ];
    expect(resolve(press("g"), registered, NO_SEQUENCE, 0)).toMatchObject({
      kind: "run",
      shortcut: { id: "bare" },
    });
  });

  it("ignores a key during IME composition", () => {
    const event = press("g");
    Object.defineProperty(event, "isComposing", { value: true });
    expect(resolve(event, all, NO_SEQUENCE, 0).kind).toBe("ignore");
  });
});

describe("documentation-only rows", () => {
  it("are listed in the overlay but never dispatched", () => {
    // §10's Board bindings are implemented by dnd-kit and TaskCard, not the
    // engine, but TITAN-FRT-048 still requires the ? overlay to list them.
    // Registering them for real would be worse than omitting them: `resolve`
    // preventDefaults before running, so a row for ArrowUp would silently kill
    // the card navigation it claims to document.
    const doc = shortcut({
      id: "board-navigate",
      keys: "ArrowUp",
      group: "Board",
      documentationOnly: true,
    });
    expect(resolve(press("ArrowUp"), [doc], NO_SEQUENCE, 0).kind).toBe(
      "ignore",
    );
    expect(byGroup([doc])).toEqual([{ group: "Board", shortcuts: [doc] }]);
  });

  it("do not shadow a real binding on the same key", () => {
    const doc = shortcut({
      id: "board-lift",
      keys: "Space",
      group: "Board",
      documentationOnly: true,
    });
    const real = shortcut({ id: "tail", keys: "Space", group: "Logs" });
    expect(
      resolve(
        press(" ", { target: document.body }),
        [doc, real],
        NO_SEQUENCE,
        0,
      ),
    ).toMatchObject({ kind: "run", shortcut: { id: "tail" } });
  });
});

describe("caps", () => {
  it("renders the platform's own modifier", () => {
    expect(caps("Mod+k", false)).toEqual([["Ctrl", "K"]]);
    expect(caps("Mod+k", true)).toEqual([["⌘", "K"]]);
  });

  it("renders a sequence as two separate steps", () => {
    // The overlay must show `G` then `O`, not `G+O` — they are pressed in turn.
    expect(caps("g o", false)).toEqual([["G"], ["O"]]);
  });

  it("uses arrows and Esc rather than DOM key names", () => {
    expect(caps("Alt+ArrowUp", false)).toEqual([["Alt", "↑"]]);
    expect(caps("Escape", false)).toEqual([["Esc"]]);
  });

  it("renders `+` as a key, not as an empty separator", () => {
    // `+` is both the modifier separator and a real key — the graph binds it
    // for zoom-in. `"+".split("+")` is ["", ""], which the overlay rendered as
    // two blank <kbd> boxes: the binding was documented as nothing at all.
    expect(caps("+", false)).toEqual([["+"]]);
    expect(caps("-", false)).toEqual([["-"]]);
    // …without breaking the ordinary case.
    expect(caps("Mod+k", false)).toEqual([["Ctrl", "K"]]);
  });
});
