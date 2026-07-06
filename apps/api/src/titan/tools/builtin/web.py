"""``web.*`` builtin tools (provider ``builtin:web``) — research search + fetch
(Vol 7 §5, risk ``read``).

Both run in the trusted Tool Host and reach the internet ONLY through the caching
fetch-gateway (they never open direct sockets): ``web.search`` queries the
pluggable backend (SearXNG by default) via the gateway; ``web.fetch`` retrieves a
page through the gateway and returns READABILITY-EXTRACTED MARKDOWN (not raw
HTML), surfacing ``from_cache`` so a repeated fetch is served from the gateway
cache. The url_allowlist SSRF guard runs on the fetch ``url`` before dispatch,
and the gateway re-checks the target after DNS/redirects (defense in depth)."""

from typing import Any

from titan.tools.constraints import url_allowlist
from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.web.extract import html_to_markdown
from titan.tools.web.session import ResearchSession


def _require_research(ctx: ToolExecutionContext) -> ResearchSession:
    if ctx.research is None:
        raise ToolError("TOOL_UNAVAILABLE", "no research gateway is configured for web tools")
    return ctx.research


async def _search(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_research(ctx)
    limit = int(args.get("limit", 8))
    result = await session.search(str(args["query"]), limit=limit)
    return ExecutionOutcome(
        result={
            "query": str(args["query"]),
            "results": result.get("results", []),
            "from_cache": bool(result.get("from_cache", False)),
        }
    )


async def _fetch(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_research(ctx)
    fetched = await session.fetch(str(args["url"]))
    content_type = str(fetched.get("content_type", "")).lower()
    body = str(fetched.get("body", ""))
    if "html" in content_type or (not content_type and "<html" in body[:2000].lower()):
        extracted = html_to_markdown(body, url=str(fetched.get("final_url") or args["url"]))
        markdown, title = extracted["markdown"], extracted["title"]
    else:
        # Non-HTML (JSON, plain text, etc.) is returned as-is, capped.
        markdown, title = body[:200_000], ""
    return ExecutionOutcome(
        result={
            "url": str(fetched.get("final_url") or args["url"]),
            "title": title,
            "content_type": content_type,
            "from_cache": bool(fetched.get("from_cache", False)),
            "markdown": markdown,
        }
    )


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


SPECS = (
    ToolSpec(
        name="web.search",
        provider="builtin:web",
        description="Search the web via the pluggable backend (SearXNG default).",
        risk="read",
        required_scopes=("web:search",),
        input_schema=_obj(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2048},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ["query"],
        ),
        output_schema=_OBJ,
        executor=_search,
        timeout_s=30.0,
        rate_limit_per_min=60,
    ),
    ToolSpec(
        name="web.fetch",
        provider="builtin:web",
        description="Fetch a URL through the research proxy and return readability markdown.",
        risk="read",
        required_scopes=("web:fetch",),
        input_schema=_obj({"url": {"type": "string", "minLength": 1, "maxLength": 4096}}, ["url"]),
        output_schema=_OBJ,
        executor=_fetch,
        timeout_s=45.0,
        rate_limit_per_min=60,
        constraints=(url_allowlist("url"),),
    ),
)

__all__ = ["SPECS"]
