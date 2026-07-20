import {
  ALLOWED_CHORDS,
  isBehindOverlay,
  isChord,
  isNativeActivation,
  isOverlayOpen,
  isSequenceLive,
  isTextEntry,
  NO_SEQUENCE,
  SEQUENCE_TIMEOUT_MS,
  tokenFor,
  type SequenceState,
} from "./keys";

/**
 * The shortcut table and the pure resolver over it (Vol 4 §10).
 *
 * The resolver is deliberately total and side-effect free: it takes an event,
 * the currently registered shortcuts and the sequence state, and returns what
 * SHOULD happen. `provider.tsx` is the only place that then actually calls
 * `run()` and mutates state — so every rule below is unit-testable without a
 * DOM, and the `?` overlay is generated from the same table that dispatches
 * (TITAN-FRT-048 requires those two to agree, and the only way to guarantee it
 * is to have one source).
 */

/** §10 groups the table into global and screen-scoped halves. */
export type ShortcutGroup =
  "Global" | "Navigation" | "Board" | "Graph" | "Logs" | "Conversations";

export interface Shortcut {
  /** Stable identity; also the React key in the overlay. */
  readonly id: string;
  /** `Mod+k`, `?`, `Alt+ArrowUp`, or a two-step sequence like `g o`. */
  readonly keys: string;
  readonly label: string;
  readonly group: ShortcutGroup;
  /**
   * Fires even while focus is inside a text field. §10 allows exactly two:
   * `Esc` and `Ctrl/⌘+Enter`.
   */
  readonly allowInTextEntry?: boolean;
  /** Fires even while a modal overlay covers the page (Esc, and Esc only). */
  readonly allowBehindOverlay?: boolean;
  /**
   * Listed in the `?` overlay but NEVER dispatched.
   *
   * §10 specifies bindings that the engine must not own — the Board's arrows,
   * Enter and Space are implemented by dnd-kit's KeyboardSensor and TaskCard's
   * own handler. TITAN-FRT-048 still requires them to appear in the overlay,
   * and registering them normally would be actively harmful: `resolve` calls
   * preventDefault() before running a match, so a documentation row for
   * `ArrowUp` would silently kill arrow navigation on the screen it documents.
   */
  readonly documentationOnly?: boolean;
  readonly run: (event: KeyboardEvent) => void;
}

export function isSequence(keys: string): boolean {
  return keys.includes(" ");
}

/**
 * Rejects a shortcut TITAN must not claim.
 *
 * Called at registration so a bad binding fails loudly in development instead
 * of silently stealing a browser accelerator from the user in production.
 */
export function validate(shortcut: Shortcut): string | null {
  const steps = shortcut.keys.split(" ");
  if (steps.length > 2) {
    return `${shortcut.id}: sequences are at most two steps`;
  }
  if (steps.length === 2) {
    if (steps.some(isChord)) {
      return `${shortcut.id}: a sequence step may not carry a modifier`;
    }
    return null;
  }
  if (
    isChord(shortcut.keys) &&
    !(ALLOWED_CHORDS as readonly string[]).includes(shortcut.keys)
  ) {
    return `${shortcut.id}: ${shortcut.keys} is not an allowed chord (§10: never override browser defaults)`;
  }
  return null;
}

export type Resolution =
  /** Run this shortcut, then adopt `sequence`. */
  | {
      readonly kind: "run";
      readonly shortcut: Shortcut;
      readonly sequence: SequenceState;
    }
  /** A sequence has started (or restarted); swallow the key. */
  | { readonly kind: "prefix"; readonly sequence: SequenceState }
  /** Nothing matched; let the browser have the key. */
  | { readonly kind: "ignore"; readonly sequence: SequenceState };

const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph"]);

/**
 * Decide what a keydown means.
 *
 * Order matters and encodes §10 directly:
 *  1. a bare modifier press never disturbs an in-flight sequence;
 *  2. a LIVE sequence only ever completes or cancels — an unmatched second key
 *     cancels WITHOUT also firing as its own shortcut, because `g` then `l`
 *     silently doing something unrelated is worse than doing nothing;
 *  3. text entry suppresses everything except `allowInTextEntry`;
 *  4. an open overlay suppresses everything except `allowBehindOverlay`;
 *  5. exact match wins over starting a new sequence.
 */
export function resolve(
  event: KeyboardEvent,
  shortcuts: readonly Shortcut[],
  state: SequenceState,
  now: number,
  timeoutMs: number = SEQUENCE_TIMEOUT_MS,
): Resolution {
  if (event.isComposing || MODIFIER_KEYS.has(event.key)) {
    return { kind: "ignore", sequence: state };
  }

  const token = tokenFor(event);

  // Space/Enter on a focused button belong to the button. Checked before
  // anything else: a bare `Space` binding that wins here makes every control on
  // that screen dead to the keyboard (TITAN-FRT-042).
  if (isNativeActivation(token, event.target)) {
    return { kind: "ignore", sequence: NO_SEQUENCE };
  }

  const inText = isTextEntry(event.target);
  // Prefer the target's own document (correct inside an iframe), but fall back
  // to the ambient one: a keydown can arrive with a null target — dispatched at
  // `window`, or synthesised — and silently skipping the overlay check there
  // would reopen exactly the hole this closes.
  const doc =
    (event.target instanceof Node ? event.target.ownerDocument : null) ??
    (typeof document === "undefined" ? null : document);
  const behindOverlay =
    isBehindOverlay(event.target instanceof Element ? event.target : null) ||
    isOverlayOpen(doc);

  // §10 says shortcuts are "disabled inside text inputs except Esc/⌘Enter".
  // The rule that intent expresses is about PRINTABLE keys: `g` must be a
  // letter while typing, `/` must be a slash. A Ctrl/⌘ chord cannot be confused
  // with typing, and reading the sentence literally would mean ⌘K does not open
  // the palette from a filter box — and, worse, cannot close it again, since
  // the palette puts focus in its own input. So chords are exempt, and the
  // allowlist in `keys.ts` is what keeps that exemption narrow.
  const suppressedByText = inText && !isChord(token);
  const eligible = shortcuts.filter(
    (shortcut) =>
      !shortcut.documentationOnly &&
      (!suppressedByText || shortcut.allowInTextEntry) &&
      (!behindOverlay || shortcut.allowBehindOverlay),
  );

  if (isSequenceLive(state, now, timeoutMs)) {
    const completion = eligible.find(
      (shortcut) => shortcut.keys === `${state.prefix} ${token}`,
    );
    return completion
      ? { kind: "run", shortcut: completion, sequence: NO_SEQUENCE }
      : { kind: "ignore", sequence: NO_SEQUENCE };
  }

  // A sequence never starts from a repeat: holding `g` down must not arm the
  // prefix over and over and swallow the key the user eventually wants.
  const exact = eligible.find((shortcut) => shortcut.keys === token);
  if (exact) {
    return { kind: "run", shortcut: exact, sequence: NO_SEQUENCE };
  }

  if (
    !event.repeat &&
    !isChord(token) &&
    eligible.some(
      (shortcut) =>
        isSequence(shortcut.keys) && shortcut.keys.startsWith(`${token} `),
    )
  ) {
    return { kind: "prefix", sequence: { prefix: token, at: now } };
  }

  return { kind: "ignore", sequence: NO_SEQUENCE };
}

// --- presentation -----------------------------------------------------------

const DISPLAY: Record<string, string> = {
  Mod: "Ctrl",
  Enter: "Enter",
  Escape: "Esc",
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
  Space: "Space",
};

/**
 * Human-readable key caps for the `?` overlay: `Mod+k` → `["Ctrl", "K"]`.
 * `mac` swaps the Ctrl cap for ⌘ — the same platform switch `tokenFor` collapses.
 */
export function caps(keys: string, mac: boolean): string[][] {
  return keys.split(" ").map((step) => {
    // `+` is BOTH the modifier separator and a real key — Graph binds it for
    // zoom-in. A naive `"+".split("+")` yields ["", ""], which the overlay
    // rendered as two EMPTY key caps, documenting the binding as two blank
    // boxes. Dropping the empty parts and falling back to the literal keeps
    // `Mod+k` and `+` both correct.
    const parts = step.split("+").filter((part) => part !== "");
    return (parts.length === 0 ? ["+"] : parts).map((part) => {
      if (part === "Mod") {
        return mac ? "⌘" : "Ctrl";
      }
      return DISPLAY[part] ?? (part.length === 1 ? part.toUpperCase() : part);
    });
  });
}

/** The overlay's sections, in §10's order, skipping groups with no bindings. */
export const GROUP_ORDER: readonly ShortcutGroup[] = [
  "Global",
  "Navigation",
  "Board",
  "Graph",
  "Logs",
  "Conversations",
];

export function byGroup(
  shortcuts: readonly Shortcut[],
): { group: ShortcutGroup; shortcuts: Shortcut[] }[] {
  return GROUP_ORDER.map((group) => ({
    group,
    shortcuts: shortcuts.filter((shortcut) => shortcut.group === group),
  })).filter((section) => section.shortcuts.length > 0);
}
