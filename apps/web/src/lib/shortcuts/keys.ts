/**
 * Pure key-matching for the global shortcut engine (Vol 4 §10, TITAN-FRT-048).
 *
 * Everything here is a function of an event and a state value — no DOM
 * registration, no React — because the interesting rules are all edge cases
 * (what counts as a text input, when a sequence expires, which chords we are
 * allowed to claim at all) and they are far cheaper to pin down in unit tests
 * than through a browser.
 */

/** §10: "sequences time out after 1.5 s". */
export const SEQUENCE_TIMEOUT_MS = 1500;

/**
 * The ONLY modifier chords TITAN may claim.
 *
 * §10 says "never override browser/system defaults", and the way that rule gets
 * broken is not deliberately — it is by someone adding `Mod+S` or `Mod+T` later
 * and taking Save or New Tab away from the user. So the allowlist is enforced at
 * registration (see `registry.ts`) rather than left to reviewer vigilance. All
 * three below are chords no major browser binds:
 *   Mod+K      — nothing (Firefox's old search focus is now Mod+E/Mod+K on some
 *                builds; we accept that, it is the industry-standard palette key)
 *   Mod+P      — PRINT. Deliberately claimed: §2.3 and §10 both specify it for
 *                the project switcher. This is the one documented exception.
 *   Mod+Enter  — nothing.
 */
export const ALLOWED_CHORDS = ["Mod+k", "Mod+p", "Mod+Enter"] as const;

export type Chord = (typeof ALLOWED_CHORDS)[number];

/**
 * Canonical token for an event: `Mod+k`, `Alt+ArrowUp`, `?`, `Space`, `g`.
 *
 * `Mod` collapses Meta (macOS) and Control (everything else) so a binding is
 * written once. Single printable keys are lower-cased so `G` and `g` are one
 * binding, but `?` — which IS Shift+/ on most layouts — keeps its printed form,
 * because that is what the user pressed and what the overlay must show.
 */
export function tokenFor(event: KeyboardEvent): string {
  const parts: string[] = [];
  if (event.metaKey || event.ctrlKey) {
    parts.push("Mod");
  }
  if (event.altKey) {
    parts.push("Alt");
  }
  // Shift is deliberately NOT a modifier token: the key it produces already
  // encodes it (`?`, `+`). Treating it as one would mean binding both `?` and
  // `Shift+/` and getting the layout wrong on non-US keyboards.
  const key = event.key === " " ? "Space" : event.key;
  parts.push(key.length === 1 ? key.toLowerCase() : key);
  return parts.join("+");
}

/** True when the token carries a Ctrl/Meta modifier. */
export function isChord(token: string): boolean {
  return token.startsWith("Mod+");
}

const TEXT_INPUT_SELECTOR =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="searchbox"], [role="combobox"]';

/**
 * Widgets that consume printable characters for their OWN type-ahead.
 *
 * An open Radix Select, DropdownMenu or the palette's own listbox is not a text
 * field, but typing in one jumps to a matching item — so a bare `g`, `n` or `?`
 * reaching the shortcut engine from inside one both swallows the keystroke and
 * fires an unrelated action. These are not covered by the `aria-hidden` overlay
 * check either: the open widget is the part of the page that is NOT hidden.
 */
const TYPEAHEAD_SELECTOR =
  '[role="listbox"], [role="menu"], [role="menubar"], [role="tree"], [role="grid"], [role="option"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"]';

/**
 * True when the target consumes typed characters, so the shortcut engine must
 * keep its hands off them.
 *
 * Walks ancestors rather than testing `tagName`: the three hand-rolled listeners
 * this engine replaces each used a slightly different tag check, and every one
 * of them missed `contenteditable` and Radix's `role="combobox"` triggers — so a
 * `/` typed into a filter box would have jumped focus somewhere else.
 *
 * `<input type=checkbox|radio|button>` is NOT text entry; excluding it would
 * make every shortcut dead while a checkbox has focus.
 */
export function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) {
    return false;
  }
  if (target.closest(TYPEAHEAD_SELECTOR)) {
    return true;
  }
  const field = target.closest(TEXT_INPUT_SELECTOR);
  if (!field) {
    return false;
  }
  if (field instanceof HTMLInputElement) {
    return ![
      "checkbox",
      "radio",
      "button",
      "submit",
      "reset",
      "range",
    ].includes(field.type);
  }
  return true;
}

/**
 * True when `element` sits inside a subtree an open overlay has hidden.
 *
 * Radix marks the rest of the page `aria-hidden="true"` while a modal is open.
 * A screen-scoped binding that fires from under a dialog reaches a screen the
 * user cannot see — Phase 60 shipped exactly that bug on Conversations, which is
 * why the original of this helper exists.
 *
 * NOTE the argument: this asks about an element of the SCREEN, not about the
 * event target. Aimed at the target it is nearly always false, because Radix
 * moves focus INTO the dialog — the one subtree that is not hidden. Use
 * {@link isOverlayOpen} for the "is anything modal on top of me" question.
 */
export function isBehindOverlay(element: Element | null): boolean {
  return element?.closest('[aria-hidden="true"]') != null;
}

/**
 * True when a modal dialog or sheet is currently open anywhere on the page.
 *
 * This is the check that actually protects screen-scoped bindings, and it took
 * three screens hand-rolling their own variant before that was obvious. The
 * intuitive guard — "is the event target inside a hidden subtree" — can never
 * fire, because the modal is precisely the part of the page Radix leaves
 * visible and focused. So the engine asks the global question instead: with a
 * dialog open, `g l` must not navigate the page underneath it.
 *
 * Keyed on the primitives' own `data-slot` + `data-state`, which `ui/dialog.tsx`
 * and `ui/sheet.tsx` set. Radix unmounts closed content, so the `data-state`
 * filter matters only during the closing animation — where suppressing is still
 * the right answer.
 */
export function isOverlayOpen(doc: Document | null | undefined): boolean {
  return (
    doc?.querySelector(
      '[data-slot="dialog-content"][data-state="open"], [data-slot="sheet-content"][data-state="open"]',
    ) != null
  );
}

/**
 * Controls for which `Space` (and `Enter`) are the NATIVE activation key.
 *
 * `a[href]` is deliberately absent: Space on a link scrolls, it does not follow.
 */
const NATIVE_ACTIVATION_SELECTOR =
  'button, summary, [role="button"], [role="checkbox"], [role="switch"], [role="radio"], [role="tab"]';

/**
 * True when this keypress is the focused control's own activation key, and so
 * belongs to the control rather than to a shortcut.
 *
 * Without this, registering §10's bare `Space` for "pause tail" makes EVERY
 * button on that screen dead to the keyboard — the engine preventDefaults
 * before dispatching, so tabbing to Export and pressing Space silently toggles
 * the log tail instead of pressing the button. That is a direct TITAN-FRT-042
 * failure ("full keyboard operability") caused by satisfying §10 naively.
 */
export function isNativeActivation(
  token: string,
  target: EventTarget | null,
): boolean {
  if (token !== "Space" && token !== "Enter") {
    return false;
  }
  if (!(target instanceof Element)) {
    return false;
  }
  // `closest`, not a tag test: the focusable element is frequently an ancestor
  // of what the browser reports — a <button> wrapping an icon, a Radix trigger
  // wrapping a span.
  return target.closest(NATIVE_ACTIVATION_SELECTOR) != null;
}

// --- sequences --------------------------------------------------------------

export interface SequenceState {
  /** The leading key of an in-progress sequence, e.g. `g`. */
  readonly prefix: string | null;
  /** When the prefix was pressed (ms since epoch). */
  readonly at: number;
}

export const NO_SEQUENCE: SequenceState = { prefix: null, at: 0 };

export function isSequenceLive(
  state: SequenceState,
  now: number,
  timeoutMs: number = SEQUENCE_TIMEOUT_MS,
): boolean {
  if (state.prefix === null) {
    return false;
  }
  // Strictly greater: at exactly 1.5 s the sequence is dead. §10 says "time out
  // AFTER 1.5 s", and a boundary that admits the keypress makes the timeout
  // untestable without sleeping for an extra frame.
  return now - state.at < timeoutMs;
}
