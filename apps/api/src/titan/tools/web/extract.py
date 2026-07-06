"""Readability extraction: raw HTML -> main-content markdown (Phase 23; INT §5).

``web.fetch`` returns readability-extracted markdown, NOT raw HTML. readability
(the Arc90 algorithm, via readability-lxml) strips nav/ads/boilerplate to the
main article HTML; markdownify converts that to markdown with real heading/link/
code markup. Pure function — unit-tested with no network."""

from typing import Any

from bs4 import BeautifulSoup
from markdownify import markdownify
from readability import Document

# Non-content elements dropped before conversion (readability keeps semantic
# nav/footer; markdownify's `strip` keeps their text — so we decompose them).
_BOILERPLATE = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")


def html_to_markdown(html: str, *, url: str = "") -> dict[str, Any]:
    """Extract the main content of ``html`` as markdown plus its title.

    Falls back to converting the whole document when readability finds no
    distinct article (very short/odd pages), so the tool always returns
    *something* usable rather than empty."""
    document = Document(html, url=url or None)
    try:
        title = (document.short_title() or "").strip()
    except Exception:  # noqa: BLE001 — readability can raise on degenerate HTML
        title = ""
    try:
        content_html = document.summary(html_partial=True)
    except Exception:  # noqa: BLE001
        content_html = html
    markdown = _to_markdown(content_html)
    if not markdown.strip():
        markdown = _to_markdown(html)
    return {"title": title, "markdown": markdown.strip(), "url": url}


def _to_markdown(content_html: str) -> str:
    soup = BeautifulSoup(content_html, "html.parser")
    for element in soup.find_all(_BOILERPLATE):
        element.decompose()  # remove the tag AND its text entirely
    text: str = markdownify(
        str(soup),
        heading_style="ATX",
        escape_asterisks=False,
        escape_underscores=False,
    )
    # Collapse the runs of blank lines markdownify tends to emit.
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


__all__ = ["html_to_markdown"]
