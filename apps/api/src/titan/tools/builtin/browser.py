"""``browser.*`` builtin tools (provider ``builtin:browser``) — headless Chromium
via Playwright, in its own container (Vol 7 §5, TITAN-INT-032).

All six tools drive ONE page per task (persistent across calls): ``goto`` then
``snapshot`` then ``click`` operate on the same page. ``snapshot`` returns the
token-efficient accessibility tree (role/name/ref); ``click``/``type`` target an
element by the ``ref`` snapshot stamped on it, so the model never handles raw
selectors. ``screenshot`` returns a base64 PNG for vision-capable models."""

import base64
import re
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from titan.tools.browser.session import BrowserSession
from titan.tools.browser.snapshot import SNAPSHOT_JS, count_nodes
from titan.tools.constraints import host_is_blocked
from titan.tools.errors import ToolError, ToolErrorCode
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec

_REF_RE = re.compile(r"^e\d+$")
_ACTION_TIMEOUT_MS = 15_000
_NAV_TIMEOUT_MS = 30_000
# Cap the inlined screenshot so a huge capture can't bloat the result/prompt.
_MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024


def _require_browser(ctx: ToolExecutionContext) -> BrowserSession:
    if ctx.browser is None:
        raise ToolError("TOOL_UNAVAILABLE", "no browser is available for this task")
    return ctx.browser


def _ref_constraint(args: dict[str, Any]) -> "tuple[ToolErrorCode, str] | None":
    """A ref is embedded in a CSS attribute selector — it must be exactly the
    ``e<N>`` snapshot assigns, never arbitrary text that could break the selector."""
    ref = args.get("ref")
    if ref is not None and not _REF_RE.match(str(ref)):
        return ("SCHEMA_INVALID", "at '/ref': must be a snapshot ref of the form 'e<number>'")
    return None


def _url_constraint(args: dict[str, Any]) -> "tuple[ToolErrorCode, str] | None":
    """Restrict navigation to a safe scheme allowlist AND block SSRF targets
    (TITAN-SEC-073/074). Only http(s), ``data:``, and ``about:blank`` are allowed
    — a scheme parse (not a raw ``startswith('file:')``) so ``view-source:file:``,
    ``javascript:``, ``blob:``, ``chrome:``, ``filesystem:`` are all rejected — and
    http(s) hosts pointing at private/loopback/link-local/cloud-metadata addresses
    are blocked so ``browser.goto('http://169.254.169.254/…')`` can't steal creds."""
    raw = str(args.get("url", "")).strip()
    scheme = urlsplit(raw).scheme.lower()
    if scheme == "about":
        if raw.lower() != "about:blank":
            return ("SANDBOX_VIOLATION", "at '/url': only about:blank is permitted")
        return None
    if scheme == "data":
        return None  # inline render; no filesystem or network reach
    if scheme not in {"http", "https"}:
        return ("SANDBOX_VIOLATION", "at '/url': only http(s)/data/about:blank URLs are permitted")
    host = (urlsplit(raw).hostname or "").rstrip(".").lower()
    if not host:
        return ("SCHEMA_INVALID", "at '/url': url has no host")
    if host_is_blocked(host):
        return ("SANDBOX_VIOLATION", "at '/url': url targets a private/link-local/metadata address")
    return None


# --- executors ---------------------------------------------------------------


async def _goto(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    # The schema enum constrains wait_until to Playwright's 4 valid literals.
    wait_until = cast(Any, str(args.get("wait_until", "load")))
    try:
        response = await page.goto(str(args["url"]), wait_until=wait_until, timeout=_NAV_TIMEOUT_MS)
    except PlaywrightError as exc:
        raise ToolError("UPSTREAM_ERROR", f"navigation failed: {_clean(exc)}") from exc
    return ExecutionOutcome(
        result={"url": page.url, "status": response.status if response else None}
    )


async def _snapshot(_args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    tree = await _evaluate(page, SNAPSHOT_JS)
    return ExecutionOutcome(
        result={"url": page.url, "node_count": count_nodes(tree), "snapshot": tree}
    )


async def _screenshot(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    try:
        png = await page.screenshot(full_page=bool(args.get("full_page", False)))
    except PlaywrightError as exc:
        raise ToolError("UPSTREAM_ERROR", f"screenshot failed: {_clean(exc)}") from exc
    payload: dict[str, Any] = {"format": "png", "bytes": len(png), "url": page.url}
    if len(png) <= _MAX_SCREENSHOT_BYTES:
        payload["base64_png"] = base64.b64encode(png).decode("ascii")
    else:
        payload["truncated"] = True
    return ExecutionOutcome(result=payload)


async def _click(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    ref = str(args["ref"])
    try:
        await page.locator(f'[data-titan-ref="{ref}"]').click(timeout=_ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        raise ToolError("UPSTREAM_ERROR", f"could not click {ref}: {_clean(exc)}") from exc
    return ExecutionOutcome(result={"clicked": True, "ref": ref, "url": page.url})


async def _type(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    ref, text = str(args["ref"]), str(args["text"])
    locator = page.locator(f'[data-titan-ref="{ref}"]')
    try:
        await locator.fill(text, timeout=_ACTION_TIMEOUT_MS)
        if args.get("submit"):
            await locator.press("Enter", timeout=_ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        raise ToolError("UPSTREAM_ERROR", f"could not type into {ref}: {_clean(exc)}") from exc
    return ExecutionOutcome(result={"typed": True, "ref": ref})


async def _eval_js(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    page = await _require_browser(ctx).page()
    value = await _evaluate(page, str(args["expression"]))
    return ExecutionOutcome(result={"result": value})


async def _evaluate(page: Page, expression: str) -> Any:
    try:
        return await page.evaluate(expression)
    except PlaywrightError as exc:
        raise ToolError("UPSTREAM_ERROR", f"eval_js failed: {_clean(exc)}") from exc


def _clean(exc: PlaywrightError) -> str:
    return str(getattr(exc, "message", exc)).splitlines()[0][:300]


# --- specs -------------------------------------------------------------------

_OBJ = {"type": "object"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _spec(
    name: str,
    description: str,
    executor: Any,
    schema: dict[str, Any],
    *,
    constraints: tuple[Any, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:browser",
        description=description,
        risk="write",
        required_scopes=("browser:use",),
        input_schema=schema,
        output_schema=_OBJ,
        executor=executor,
        timeout_s=60.0,
        rate_limit_per_min=120,
        constraints=constraints,
    )


SPECS = (
    _spec(
        "browser.goto",
        "Navigate the task's page to a URL.",
        _goto,
        _obj(
            {
                "url": {"type": "string", "minLength": 1, "maxLength": 4096},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                },
            },
            ["url"],
        ),
        constraints=(_url_constraint,),
    ),
    _spec(
        "browser.snapshot",
        "Return the token-efficient accessibility tree (role/name/ref) of the page.",
        _snapshot,
        _obj({}, []),
    ),
    _spec(
        "browser.screenshot",
        "Capture a PNG screenshot (base64) for vision-capable models.",
        _screenshot,
        _obj({"full_page": {"type": "boolean"}}, []),
    ),
    _spec(
        "browser.click",
        "Click the element identified by a snapshot ref.",
        _click,
        _obj({"ref": {"type": "string", "minLength": 1, "maxLength": 32}}, ["ref"]),
        constraints=(_ref_constraint,),
    ),
    _spec(
        "browser.type",
        "Type text into the element identified by a snapshot ref.",
        _type,
        _obj(
            {
                "ref": {"type": "string", "minLength": 1, "maxLength": 32},
                "text": {"type": "string", "maxLength": 100_000},
                "submit": {"type": "boolean"},
            },
            ["ref", "text"],
        ),
        constraints=(_ref_constraint,),
    ),
    _spec(
        "browser.eval_js",
        "Evaluate a JavaScript expression in the page and return its JSON result.",
        _eval_js,
        _obj(
            {"expression": {"type": "string", "minLength": 1, "maxLength": 100_000}}, ["expression"]
        ),
    ),
)

__all__ = ["SPECS"]
