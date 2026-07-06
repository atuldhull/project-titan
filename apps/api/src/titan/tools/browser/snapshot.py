"""The token-efficient accessibility snapshot (TITAN-INT-032).

Playwright 1.61 dropped the legacy ``page.accessibility`` API, and we need
stable element *refs* for click/type anyway — so we inject a DOM walker that, in
one round-trip, computes each element's ARIA role + accessible name, stamps a
``data-titan-ref`` on every element that carries a role, and returns a compact
nested tree of ``{role, name, ref, children}``. Later ``browser.click(ref)`` /
``browser.type(ref, text)`` locate the element via ``[data-titan-ref="<ref>"]``.

Emitting only role/name/ref (never raw HTML/markup) is what keeps the snapshot
under 10% of the page's raw-HTML token count for the QA/Research agents.
"""

from typing import Any

# A single-expression JS function Playwright's page.evaluate runs in the page.
# Deterministic ref numbering (pre-order); skips invisible / script / style nodes.
SNAPSHOT_JS = r"""
() => {
  let counter = 0;
  const IMPLICIT = {
    A: 'link', BUTTON: 'button', H1: 'heading', H2: 'heading', H3: 'heading',
    H4: 'heading', H5: 'heading', H6: 'heading', NAV: 'navigation', MAIN: 'main',
    HEADER: 'banner', FOOTER: 'contentinfo', UL: 'list', OL: 'list',
    LI: 'listitem', SELECT: 'combobox', TEXTAREA: 'textbox', IMG: 'img',
    FORM: 'form', TABLE: 'table', P: 'paragraph', LABEL: 'label',
    SECTION: 'region', ARTICLE: 'article'
  };
  const inputRole = (el) => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'checkbox') return 'checkbox';
    if (t === 'radio') return 'radio';
    if (t === 'button' || t === 'submit' || t === 'reset') return 'button';
    if (t === 'hidden') return null;
    return 'textbox';
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim().split(/\s+/)[0];
    if (el.tagName === 'INPUT') return inputRole(el);
    return IMPLICIT[el.tagName] || null;
  };
  // ARIA "name from content" roles compute their name from ALL descendant text
  // (a <button><span>Place Order</span></button> is named "Place Order"); other
  // roles (landmarks, generic containers) only take their own direct text so
  // descendant content doesn't balloon the tree.
  const NAME_FROM_CONTENT = new Set([
    'link', 'button', 'heading', 'listitem', 'option', 'tab', 'menuitem', 'cell',
  ]);
  const nameOf = (el, role) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 200);
    if (el.tagName === 'IMG') return (el.getAttribute('alt') || '').trim().slice(0, 200);
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
      return (el.getAttribute('placeholder') || el.getAttribute('value') || '')
        .trim().slice(0, 200);
    if (el.children.length === 0) return (el.textContent || '').trim().slice(0, 200);
    if (NAME_FROM_CONTENT.has(role)) return (el.textContent || '').trim().slice(0, 200);
    // Container role: only its own direct text (avoid duplicating descendant text).
    let direct = '';
    for (const node of el.childNodes)
      if (node.nodeType === 3) direct += node.textContent;
    return direct.trim().slice(0, 200);
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    return true;
  };
  const walk = (el) => {
    const out = [];
    for (const child of el.children) {
      if (child.tagName === 'SCRIPT' || child.tagName === 'STYLE' || !visible(child)) continue;
      const role = roleOf(child);
      const kids = walk(child);
      if (role) {
        const ref = 'e' + (counter++);
        child.setAttribute('data-titan-ref', ref);
        // Omit empty name / children so the tree stays token-lean (TITAN-INT-032).
        const node = { role, ref };
        const name = nameOf(child, role);
        if (name) node.name = name;
        if (kids.length) node.children = kids;
        out.push(node);
      } else {
        // Roleless wrapper (div/span): hoist its children so the tree stays flat.
        for (const k of kids) out.push(k);
      }
    }
    return out;
  };
  return { role: 'document', name: document.title || '', children: walk(document.body) };
}
"""


def count_nodes(tree: dict[str, Any]) -> int:
    """Total nodes in a snapshot tree (root included)."""
    children = tree.get("children") or []
    total = 1
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                total += count_nodes(child)
    return total


__all__ = ["SNAPSHOT_JS", "count_nodes"]
