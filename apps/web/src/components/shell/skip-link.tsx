/**
 * Skip-to-content (TITAN-FRT-042).
 *
 * Visually hidden until focused, then pinned at the top-left — so the first Tab
 * on every screen offers a way past the sidebar's fifteen nav links instead of
 * making a keyboard user walk them on every navigation.
 *
 * It is a plain `<a href="#main">`, not a `focus()` call: the hash also moves
 * the browser's sequential-navigation point, which a scripted focus does not do
 * reliably across engines.
 */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only rounded-md bg-card px-4 py-2 text-sm font-medium text-foreground outline-2 outline-ring outline-offset-2 focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-100 focus:border"
    >
      Skip to content
    </a>
  );
}
