/**
 * Fuzzy ranking for the command palette (TITAN-FRT-007).
 *
 * The acceptance criterion is literal: typing `kan net` must surface the
 * netflix-clone board. That is two terms matching two DIFFERENT words of one
 * item ("Board" / "netflix-clone"), so a single-substring match is not enough —
 * every whitespace-separated term has to match independently, and the item's
 * score is their sum.
 *
 * Matching is subsequence-based with a word-start bonus, so `kan` beats a
 * scattered `k…a…n` and `net` scores higher against "netflix-clone" than
 * against "assig-n-m-e-nt". The DP is O(term × text) per term — with a few
 * hundred candidates and short strings that is well inside the 100 ms budget
 * §1.4 puts on opening the palette.
 */

/** A word-start match is worth far more than a mid-word one. */
const START_BONUS = 12;
/** Adjacent characters are what makes a match feel like a prefix. */
const CONTIGUOUS_BONUS = 8;
/** Each skipped character costs a little, so tight matches sort first. */
const GAP_PENALTY = 1;

const SEPARATORS = new Set([" ", "-", "_", "/", ".", ":", "(", "["]);

function isWordStart(text: string, index: number): boolean {
  if (index === 0) {
    return true;
  }
  const previous = text[index - 1];
  if (SEPARATORS.has(previous)) {
    return true;
  }
  // camelCase / PascalCase boundary.
  return (
    previous === previous.toLowerCase() &&
    text[index] !== text[index].toLowerCase()
  );
}

/**
 * Case-fold WITHOUT changing the string's length.
 *
 * `String.prototype.toLowerCase` is not length-preserving: U+0130 (İ, Turkish
 * dotted capital I) lowercases to TWO code units, "i" + U+0307. The DP below
 * indexes the ORIGINAL `text` with positions derived from the lowercased copy
 * (word-boundary detection needs the original case), so a single such character
 * anywhere in a project, agent or task name shifted every subsequent index —
 * silently corrupting the ranking, and reading past the end of `text` inside
 * `isWordStart`, which throws on `text[index].toLowerCase()` of `undefined`.
 * That throw happens during the palette's render, so one Turkish project name
 * took down the whole dashboard shell as soon as the user typed.
 *
 * Folding per code unit keeps the two strings index-aligned. The cost is that
 * a character whose lowercase form is a different length no longer folds — İ
 * stays İ and only matches İ — which is a far better failure than a crash, and
 * only affects the handful of characters with length-changing case mappings.
 */
function lowerPreservingLength(value: string): string {
  let out = "";
  for (const unit of value) {
    const lowered = unit.toLowerCase();
    out += lowered.length === unit.length ? lowered : unit;
  }
  return out;
}

/**
 * Best score for `term` as a subsequence of `text`, or null when it does not
 * occur at all. Both are matched case-insensitively; `text` is passed in its
 * original case so word-boundary detection can see camelCase.
 */
export function scoreTerm(term: string, text: string): number | null {
  if (term.length === 0) {
    return 0;
  }
  if (term.length > text.length) {
    return null;
  }
  const needle = lowerPreservingLength(term);
  const haystack = lowerPreservingLength(text);

  // previous[j] = best score for needle[0..i-1] ending exactly at text[j].
  let previous: number[] = [];
  for (let i = 0; i < needle.length; i += 1) {
    const current: number[] = new Array(haystack.length).fill(
      Number.NEGATIVE_INFINITY,
    );
    // gapBest[j] = best previous[k] for k <= j-2, decayed by the gap length.
    let gapBest = Number.NEGATIVE_INFINITY;
    for (let j = 0; j < haystack.length; j += 1) {
      if (j >= 1) {
        gapBest = Math.max(
          gapBest - GAP_PENALTY,
          previous[j - 1] - GAP_PENALTY,
        );
      }
      if (haystack[j] !== needle[i]) {
        continue;
      }
      let base: number;
      if (i === 0) {
        base = 0;
      } else {
        const contiguous =
          j >= 1
            ? previous[j - 1] + CONTIGUOUS_BONUS
            : Number.NEGATIVE_INFINITY;
        base = Math.max(contiguous, gapBest);
      }
      if (base === Number.NEGATIVE_INFINITY) {
        continue;
      }
      current[j] = base + (isWordStart(text, j) ? START_BONUS : 0);
    }
    previous = current;
  }
  const best = Math.max(...previous);
  return best === Number.NEGATIVE_INFINITY ? null : best;
}

export interface Searchable {
  /** Everything the query may match: title, subtitle, keywords, all joined. */
  readonly haystack: string;
}

/**
 * Score an item against a whole query. Null when ANY term fails — an item that
 * matches "kan" but not "net" is not a result for `kan net`.
 */
export function scoreQuery(query: string, item: Searchable): number | null {
  const terms = query.trim().split(/\s+/).filter(Boolean);
  if (terms.length === 0) {
    return 0;
  }
  let total = 0;
  for (const term of terms) {
    const termScore = scoreTerm(term, item.haystack);
    if (termScore === null) {
      return null;
    }
    total += termScore;
  }
  return total;
}

/**
 * Rank items best-first. An empty query keeps the caller's own order, which is
 * what makes the palette's opening state a curated list of screens rather than
 * an arbitrary permutation.
 */
export function rank<T extends Searchable>(
  query: string,
  items: readonly T[],
): T[] {
  if (query.trim() === "") {
    return [...items];
  }
  return (
    items
      .map((item, index) => ({ item, index, score: scoreQuery(query, item) }))
      .filter(
        (scored): scored is { item: T; index: number; score: number } =>
          scored.score !== null,
      )
      // Ties break on the caller's order so ranking is stable across renders —
      // an unstable palette moves the row out from under the user's Enter.
      .sort((a, b) => b.score - a.score || a.index - b.index)
      .map((scored) => scored.item)
  );
}
