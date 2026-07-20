/**
 * What a bare `Space` should do on the Logs screen (Vol 4 §10: "Logs — Space
 * pause tail").
 *
 * This exists because of a gap between §10's two halves. The global shortcut
 * engine suppresses screen bindings inside TEXT ENTRY (`isTextEntry` in
 * `lib/shortcuts/keys`) and behind overlays, which is right and is why this
 * screen no longer hand-rolls either check. But Space and Enter are also the
 * NATIVE activation keys of a focused `<button>`, and the engine calls
 * `preventDefault()` before it runs a matched shortcut — so registering a bare
 * `Space` makes every button on the screen dead to the keyboard: Export, Wrap,
 * Tailing, each level chip, each gap's [load]. A user tabbing to Export and
 * pressing Space would toggle the tail instead, with no other feedback. That is
 * a straight TITAN-FRT-042 failure ("full keyboard operability").
 *
 * The right place to fix it is the engine, next to `isTextEntry` — reported
 * rather than done, because `lib/shortcuts` belongs to another agent. Until
 * then this decides locally, and it is a pure function of the focused element so
 * the rules are testable without a browser.
 *
 * The three outcomes are deliberately NOT two. "Do nothing" is not an
 * acceptable answer for a focused button: the engine has already swallowed the
 * key, so returning early would leave the button silently unresponsive, which is
 * WORSE than the bug this guards. `activate` therefore hands the press back.
 */

export type SpaceOutcome =
  /** Focus is on nothing interesting: §10's binding wins. */
  | "toggle-tail"
  /** Focus is on a control Space natively CLICKS; replay that click. */
  | "activate"
  /** Focus is on a control Space does not activate (a link, which Space
   *  page-scrolls). The engine already ate the scroll; do nothing rather than
   *  invent a navigation the user did not ask for. */
  | "ignore";

/**
 * Roles and elements whose native Space behaviour is "click me".
 *
 * `a[href]` is deliberately absent: Space on a link scrolls, it does not
 * follow — clicking one here would navigate on a key that never navigates.
 * It is caught by IGNORE_SELECTOR instead.
 */
const ACTIVATE_SELECTOR =
  'button, summary, [role="button"], [role="checkbox"], [role="switch"], [role="radio"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="tab"], [role="option"]';

const IGNORE_SELECTOR = "a[href]";

export function spaceOutcome(focused: Element | null): SpaceOutcome {
  if (!focused) {
    return "toggle-tail";
  }
  // `closest`, not a tag test: the focusable element is frequently an ancestor
  // of whatever the browser reports — a `<button>` wrapping an icon, a Radix
  // trigger wrapping a span — and a tagName check on the inner node misses it.
  if (focused.closest(ACTIVATE_SELECTOR)) {
    return "activate";
  }
  if (focused.closest(IGNORE_SELECTOR)) {
    return "ignore";
  }
  return "toggle-tail";
}
