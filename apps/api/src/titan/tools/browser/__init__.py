"""Headless-Chromium browser tools via Playwright (Phase 22; Vol 7 §5, INT-032).

Chromium runs in its OWN container (``titan-browser``, running
``playwright run-server``); the trusted Tool Host connects to its websocket and
drives one page per task. ``browser.snapshot`` returns a token-efficient
accessibility tree (role/name/ref) — under 10% of the raw HTML's token count —
and click/type target elements by the ``ref`` that snapshot assigns."""
