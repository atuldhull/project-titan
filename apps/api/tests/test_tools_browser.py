"""browser.* tests (Phase 22; Vol 7 §5, TITAN-INT-032).

Pure units (no Docker) cover the ref/url guards and the snapshot node counter;
``sandbox``-marked tests (need Docker + titan-browser) prove the DoD — a
snapshot → click → snapshot round-trip shows the post-click state change — plus
token efficiency (<10% of raw HTML), screenshot, type, and eval_js.
"""

import json
from typing import Any

import pytest

from titan.tools.browser.snapshot import count_nodes
from titan.tools.builtin import browser as browser_tools

from ._container_tools import BrowserHarness, browser_ready
from .conftest import run_async

# --- pure unit tests (no Docker) ---------------------------------------------


def test_ref_constraint_rejects_selector_injection() -> None:
    check = browser_tools._ref_constraint
    assert check({"ref": "e12"}) is None
    for bad in ['e1"]', 'e1"><script>', "button", "e1 e2", "", "12"]:
        verdict = check({"ref": bad})
        assert verdict is not None and verdict[0] == "SCHEMA_INVALID", repr(bad)


def test_url_constraint_allows_safe_schemes_and_blocks_the_rest() -> None:
    check = browser_tools._url_constraint
    for ok in [
        "https://example.com",
        "http://docs.example.org/x",
        "data:text/html,<h1>x</h1>",
        "about:blank",
    ]:
        assert check({"url": ok}) is None, ok
    # Scheme parse (not startswith): file/view-source/javascript/blob/chrome all blocked.
    for bad in [
        "file:///etc/passwd",
        "  FILE:///etc/passwd",
        "view-source:file:///etc/hostname",
        "javascript:alert(1)",
        "blob:https://x/y",
        "chrome://settings",
        "about:config",
    ]:
        verdict = check({"url": bad})
        assert verdict is not None and verdict[0] == "SANDBOX_VIOLATION", bad


def test_url_constraint_blocks_ssrf_metadata_targets() -> None:
    # SEC-073/074: the cloud-metadata credential-theft vector is rejected, in
    # every literal-IP encoding, while public docs URLs pass.
    check = browser_tools._url_constraint
    for bad in [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/admin",
        "http://10.0.0.5/",
        "http://metadata.google.internal/",
        "http://0x7f000001/",
        "http://localhost/x",
    ]:
        verdict = check({"url": bad})
        assert verdict is not None and verdict[0] == "SANDBOX_VIOLATION", bad
    assert check({"url": "https://developer.mozilla.org/en-US/"}) is None


def test_count_nodes_counts_the_whole_tree() -> None:
    tree = {
        "role": "document",
        "children": [
            {"role": "heading", "children": []},
            {"role": "nav", "children": [{"role": "link", "children": []}]},
        ],
    }
    assert count_nodes(tree) == 4


# --- sandbox-marked DoD tests (need Docker + titan-browser) -------------------
#
# Playwright's browser/page objects are bound to the event loop that created
# them, so — unlike the sync-SDK sandbox/DinD tools — every browser scenario runs
# inside ONE asyncio loop: create the harness, drive the page, then aclose, all
# under a single run_async.


@pytest.fixture(scope="module", autouse=True)
def _require_docker(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("sandbox") and not browser_ready():
        pytest.skip("no Docker / titan-browser:local for the browser suite")


def _run_scenario(fn: Any) -> Any:
    """Run ``fn(harness)`` (a coroutine) in a single loop, closing the browser
    session in the SAME loop afterward."""

    async def wrapper() -> Any:
        harness = BrowserHarness()
        try:
            return await fn(harness)
        finally:
            await harness.manager.aclose()

    return run_async(wrapper)


async def _build_dom(harness: BrowserHarness, html: str) -> None:
    # Load a blank page, then install the fixture DOM + a click handler via a
    # real listener (robust across data:/about:blank origins).
    await browser_tools._goto({"url": "about:blank"}, harness.ctx())
    expr = (
        "() => { document.body.innerHTML = "
        + json.dumps(html)
        + "; const b = document.getElementById('b');"
        " if (b) b.addEventListener('click', () => {"
        " document.getElementById('s').textContent = 'clicked!'; }); return true; }"
    )
    await browser_tools._eval_js({"expression": expr}, harness.ctx())


def _find(node: dict[str, Any], role: str) -> dict[str, Any] | None:
    if node.get("role") == role:
        return node
    for child in node.get("children", []):
        found = _find(child, role)
        if found:
            return found
    return None


@pytest.mark.sandbox
def test_snapshot_click_snapshot_round_trip() -> None:
    # The DoD: a snapshot → click → snapshot round-trip on a fixture page shows
    # the post-click state change.
    async def scenario(h: BrowserHarness) -> None:
        await _build_dom(h, '<h1>Fixture</h1><button id="b">Press me</button><p id="s">initial</p>')
        snap1 = (await browser_tools._snapshot({}, h.ctx())).result
        status1 = _find(snap1["snapshot"], "paragraph")
        assert status1 is not None and status1["name"] == "initial"
        button = _find(snap1["snapshot"], "button")
        assert button is not None and button["name"] == "Press me"

        clicked = (await browser_tools._click({"ref": button["ref"]}, h.ctx())).result
        assert clicked["clicked"] is True

        snap2 = (await browser_tools._snapshot({}, h.ctx())).result
        status2 = _find(snap2["snapshot"], "paragraph")
        assert status2 is not None and status2["name"] == "clicked!"  # state changed

    _run_scenario(scenario)


def _reference_page() -> str:
    # A page shaped like a real one: a large block of inline critical CSS (which
    # yields ZERO accessibility nodes) plus verbose, class-heavy card markup with
    # inline SVG icons. This is what makes the a11y tree <10% of the raw HTML —
    # the same reason it holds for real reference pages (TITAN-INT-032).
    css = "\n".join(
        f".u-{i} {{ display:flex; align-items:center; gap:{i % 12}px; "
        f"padding:{i % 8}px {i % 16}px; margin:0; color:#1f2937; background:#ffffff; "
        f"border:1px solid #e5e7eb; border-radius:{i % 12}px; "
        f"box-shadow:0 1px 2px rgba(0,0,0,.05); transition:all .15s ease-in-out; }}"
        for i in range(450)
    )
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
        'stroke-width="1.5" stroke="currentColor" class="h-6 w-6 text-slate-400" '
        'aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 4.5v15m7.5-7.5h-15"/></svg>'
    )
    cards = "".join(
        f'<section class="u-{i % 40} relative isolate flex flex-col overflow-hidden '
        f"rounded-2xl border border-slate-200 bg-white px-6 py-8 shadow-sm ring-1 "
        f'ring-slate-900/5 transition hover:shadow-lg" data-testid="card-{i}" '
        f'data-index="{i}" style="min-height:140px;margin-bottom:12px">'
        f'<div class="mb-3 flex items-center justify-between gap-2">'
        f'<span class="truncate text-sm font-semibold text-slate-500">row {i}</span>'
        f"{icon}</div>"
        f'<button aria-label="act {i}" class="mt-auto inline-flex items-center '
        f"justify-center rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium "
        f'text-white hover:bg-indigo-500">Go</button></section>'
        for i in range(60)
    )
    return f"<style>{css}</style><main>{cards}</main>"


@pytest.mark.sandbox
def test_snapshot_is_token_efficient() -> None:
    # TITAN-INT-032: the accessibility snapshot is <10% of the raw HTML's size.
    async def scenario(h: BrowserHarness) -> None:
        await _build_dom(h, _reference_page())
        snap = (await browser_tools._snapshot({}, h.ctx())).result
        raw_html = (
            await browser_tools._eval_js(
                {"expression": "() => document.documentElement.outerHTML"}, h.ctx()
            )
        ).result["result"]
        snapshot_size = len(json.dumps(snap["snapshot"]))
        assert snapshot_size < 0.10 * len(raw_html), (snapshot_size, len(raw_html))
        assert snap["node_count"] >= 60  # every actionable button surfaced

    _run_scenario(scenario)


@pytest.mark.sandbox
def test_type_screenshot_and_eval_js() -> None:
    async def scenario(h: BrowserHarness) -> None:
        await _build_dom(h, '<input id="q" aria-label="Search"><p id="s">x</p>')
        snap = (await browser_tools._snapshot({}, h.ctx())).result
        box = _find(snap["snapshot"], "textbox")
        assert box is not None
        await browser_tools._type({"ref": box["ref"], "text": "hello titan"}, h.ctx())
        value = (
            await browser_tools._eval_js(
                {"expression": "() => document.getElementById('q').value"}, h.ctx()
            )
        ).result
        assert value["result"] == "hello titan"

        shot = (await browser_tools._screenshot({}, h.ctx())).result
        assert shot["format"] == "png" and shot["bytes"] > 0 and "base64_png" in shot

    _run_scenario(scenario)


@pytest.mark.sandbox
@pytest.mark.schema
def test_browser_snapshot_through_the_bus_is_audited(migrated_engine: Any) -> None:
    from sqlalchemy import text

    from titan.tools import repository as audit_repo
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call
    from titan.tools.service import build_registry

    async def scenario(h: BrowserHarness) -> str:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("TRUNCATE tool_audit"))
        await browser_tools._goto({"url": "about:blank"}, h.ctx())
        bus = ToolBus(
            build_registry(),
            migrated_engine,
            approval_gate=deny_all_approvals,
            browser_manager=h.manager,
        )
        caller = ToolCaller(
            agent_id="agent.qa.1",
            role="qa",
            project_id=h.project_id,
            granted_scopes=frozenset({"browser:use"}),
            task_id=h.task_id,
        )
        call = new_call(
            "browser.snapshot",
            {},
            project_id=h.project_id,
            agent_id=caller.agent_id,
            task_id=h.task_id,
        )
        result = await bus.invoke(call, caller)
        assert result.status == "ok" and "snapshot" in result.result
        async with migrated_engine.connect() as conn:
            audited = await audit_repo.rows_for_call(conn, call.tool_call_id)
        assert [r.status for r in audited] == ["started", "ok"]
        return "ok"

    assert _run_scenario(scenario) == "ok"
