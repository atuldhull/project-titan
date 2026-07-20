/**
 * Shared accessibility class strings for the vendored primitives (ADR-FRT-02).
 *
 * These live in one module because the bug they fix is DIVERGENCE: before this,
 * button, input, tabs, select, badge, checkbox, radio-group and switch each
 * hand-rolled their own focus treatment, and eight hand-rolled copies had
 * drifted into a treatment none of them shared and the spec never asked for.
 *
 * Tailwind v4 scans source files for class-like strings, so the literals below
 * are picked up here even though every call site interpolates the constant.
 */

/**
 * TITAN-FRT-042 / §9 — THE focus treatment for every focusable primitive.
 *
 * §9 specifies the ring as "`--ring`, 2 px offset". The primitives previously
 * used `focus-visible:ring-[3px] focus-visible:ring-ring/50` with NO offset:
 * wrong width, wrong offset, and half-transparent so it washed out against
 * `bg-primary`. A real `outline` (rather than a box-shadow `ring`) is used
 * deliberately, for ONE reason: it survives forced-colors/high-contrast mode,
 * where box-shadows are dropped entirely and a `ring-*` focus indicator simply
 * vanishes. (It is NOT about `overflow-hidden` — an ancestor's overflow clips
 * an outline and a box-shadow alike, and an element's own overflow clips
 * neither.) `--ring` is defined per theme (globals.css light + dark), so it is
 * visible in both without a `dark:` variant.
 *
 * Why `outline-none` is still here and NOT redundant with the global
 * `:focus-visible` rule in globals.css: the plain `outline-none` suppresses the
 * user-agent ring on MOUSE focus (Safari draws one on `:focus`, not only
 * `:focus-visible`). It is a utility, so it lands in Tailwind's utilities layer
 * and would beat the base-layer global rule — leaving the control with NO
 * visible keyboard ring at all. Re-stating the focus-visible outline here is
 * what keeps that from happening.
 *
 * WHY `focus-visible:outline-solid` IS LOAD-BEARING — do not "simplify" it away.
 * In Tailwind v4 the width utilities do NOT set a concrete outline style; they
 * defer to a custom property (verified against the installed tailwindcss
 * 4.3.2/4.3.3 output):
 *
 *     .outline-none              { --tw-outline-style: none; outline-style: none }
 *     .focus-visible\:outline-2  { outline-style: var(--tw-outline-style);
 *                                  outline-width: 2px }
 *
 * `--tw-outline-style` has an initial value of `solid`, but `outline-none` sets
 * it to `none` on the element UNCONDITIONALLY — it is not scoped to a state. So
 * `outline-none` + `focus-visible:outline-2` computes to `outline-style: none`
 * at 2px of nothing: the ring never paints, and because the utility also
 * outranks the base-layer `:focus-visible` rule in globals.css, the control ends
 * up with no focus indicator at all — strictly worse than the `ring-[3px]` this
 * replaced. `focus-visible:outline-solid` restores the property (and the
 * longhand) at `:focus-visible` only, where its extra pseudo-class specificity
 * beats the bare `.outline-none`, so the ring paints when focused and the UA
 * ring stays suppressed the rest of the time.
 */
export const FOCUS_RING =
  "outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/**
 * §8 responsive rule: "touch targets ≥ 44 px under `md`".
 *
 * MIN-height rather than height, and scoped to `max-md`, for two reasons:
 *  - at ≥ md every control keeps its exact current box, so this cannot move a
 *    single desktop layout (the dashboard is desktop-first and dense);
 *  - min-height lets a text-only control grow to 44 px without stretching or
 *    re-centering its label, which a taller `h-*` would do.
 *
 * 44 px = `11` on the 4 px scale (2.75rem). Every size step needs it: the
 * `xs` (24 px) and `sm` (32 px) steps are the ones a fix aimed only at the
 * `default` size silently leaves under the threshold.
 */
export const TOUCH_TARGET = "max-md:min-h-11";

/**
 * The square variant, for icon-only controls whose box is 24–40 px in BOTH
 * axes. A bare `min-h` would leave a 24 px-wide icon button still un-tappable.
 */
export const TOUCH_TARGET_ICON = "max-md:min-h-11 max-md:min-w-11";

/**
 * Hit-area expander for controls that must stay visually tiny — checkbox
 * (16 px), radio (16 px), switch (20×36 px). Their painted box IS the design;
 * growing it to 44 px would wreck every settings form. So the painted box stays
 * and an absolutely-positioned, invisible pseudo-element carries the touch
 * target instead. `-inset-3.5` = 14 px on each side, i.e. 16 + 28 = 44 px.
 *
 * `max-md:m-3.5` IS NOT COSMETIC — do not drop it to "tighten up" a form.
 *
 * A pseudo-element is painted outside its host's layout box, so on its own it
 * expands what the control CATCHES without expanding what the control OCCUPIES.
 * In any dense stack the neighbours therefore stay where they were and the
 * 44 px areas overlap: the later element in DOM order paints on top, so it
 * swallows the taps aimed at the bottom of the one above it. That is strictly
 * worse than a small target — a small target is at least unambiguous. The
 * settings notification matrix is a live example: below md it renders each
 * channel checkbox in a `grid ... gap-y-1` row roughly 24 px apart, so bare
 * pseudo-elements would have made half of every checkbox toggle its neighbour.
 *
 * The margin reserves exactly the 14 px the pseudo-element claims, so the
 * control's layout footprint becomes the full 44 px and neighbours are pushed
 * apart BY CONSTRUCTION — no consumer has to know this expander exists, which
 * is the only version of it that stays correct as screens are added. Margin
 * rather than padding because padding would stretch the painted border and
 * background; margin leaves the 16 px box untouched.
 *
 * `relative` anchors the pseudo-element. Everything is `max-md`-scoped, so at
 * ≥ md the control's box and spacing are byte-identical to before.
 */
export const TOUCH_TARGET_PSEUDO =
  "relative max-md:m-3.5 max-md:before:absolute max-md:before:-inset-3.5 max-md:before:content-['']";
