"""Hermetic tests for the web.* tools (Phase 23) — no gateway, no network. A fake
research session returns canned gateway responses so we assert the executors'
markdown extraction, from_cache surfacing, search shaping, and the SSRF guard."""

import uuid
from typing import Any

from titan.tools.builtin import web as web_tools
from titan.tools.registry import ToolExecutionContext
from titan.tools.service import build_registry

from .conftest import run_async


class _FakeResearch:
    """Stands in for ResearchSession; records calls, returns canned responses."""

    def __init__(self, fetch: dict[str, Any] | None = None, search: dict[str, Any] | None = None):
        self._fetch = fetch or {}
        self._search = search or {}
        self.fetched: list[str] = []
        self.searched: list[str] = []

    async def fetch(self, url: str) -> dict[str, Any]:
        self.fetched.append(url)
        return {"final_url": url, **self._fetch}

    async def search(self, query: str, *, limit: int) -> dict[str, Any]:
        self.searched.append(query)
        return self._search


def _ctx(research: Any) -> ToolExecutionContext:
    return ToolExecutionContext(
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        agent_id="agent.research.1",
        agent_uuid=None,
        research=research,
    )


_HTML = (
    "<html><head><title>Doc</title></head><body><nav>menu</nav>"
    "<article><h1>Guide</h1><p>Install the CLI to begin building the whole thing "
    "in a couple of minutes, then create a project and assign your first tasks.</p>"
    "</article><footer>boilerplate-footer</footer></body></html>"
)


def test_web_fetch_returns_markdown_and_surfaces_cache() -> None:
    research = _FakeResearch(
        fetch={"body": _HTML, "content_type": "text/html; charset=utf-8", "from_cache": True}
    )
    result = run_async(
        lambda: web_tools._fetch({"url": "https://x.test/guide"}, _ctx(research))
    ).result
    assert "# Guide" in result["markdown"]  # readability markdown, not raw HTML
    assert "<article>" not in result["markdown"]
    assert "boilerplate-footer" not in result["markdown"]  # boilerplate stripped
    assert result["from_cache"] is True
    assert result["title"] == "Doc" or result["title"] == "Guide"


def test_web_fetch_passes_non_html_through() -> None:
    research = _FakeResearch(
        fetch={"body": '{"ok": true}', "content_type": "application/json", "from_cache": False}
    )
    result = run_async(
        lambda: web_tools._fetch({"url": "https://x.test/api"}, _ctx(research))
    ).result
    assert result["markdown"] == '{"ok": true}'  # returned as-is


def test_web_search_shapes_results() -> None:
    research = _FakeResearch(
        search={
            "from_cache": True,
            "results": [{"title": "R1", "url": "https://a/1", "snippet": "s1"}],
        }
    )
    result = run_async(
        lambda: web_tools._search({"query": "titan", "limit": 5}, _ctx(research))
    ).result
    assert result["query"] == "titan" and result["from_cache"] is True
    assert result["results"][0]["title"] == "R1"


def test_web_tools_fail_closed_without_gateway() -> None:
    import pytest

    from titan.tools.errors import ToolError

    with pytest.raises(ToolError) as exc:
        run_async(lambda: web_tools._fetch({"url": "https://x/"}, _ctx(None)))
    assert exc.value.code == "TOOL_UNAVAILABLE"


def test_research_client_maps_gateway_errors() -> None:
    import httpx

    from titan.tools.errors import ToolError
    from titan.tools.web.session import ResearchClient

    statuses = {
        "/ok": (200, {"body": "hi", "from_cache": False}),
        "/forbidden": (403, {"detail": "private address"}),
        "/unauth": (401, {"detail": "bad creds"}),
        "/boom": (502, {"detail": "upstream down"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        code, payload = statuses[request.url.path]
        return httpx.Response(code, json=payload)

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = ResearchClient(http, gateway_url="http://gw", token="t")
        # 200 returns the JSON body.
        ok = await client._request("GET", "/ok")
        assert ok["body"] == "hi"
        # Error statuses map onto the tool taxonomy.
        for path, expected in [
            ("/forbidden", "SANDBOX_VIOLATION"),
            ("/unauth", "TOOL_UNAVAILABLE"),
            ("/boom", "UPSTREAM_ERROR"),
        ]:
            try:
                await client._request("GET", path)
                raise AssertionError(f"{path} should have raised")
            except ToolError as exc:
                assert exc.code == expected, path
        await client.aclose()

    run_async(scenario)


def test_web_fetch_ssrf_constraint_blocks_metadata() -> None:
    # web.fetch carries the url_allowlist SSRF constraint (checked in the bus).
    spec = next(s for s in build_registry().specs() if s.name == "web.fetch")
    assert spec.constraints, "web.fetch must carry an SSRF constraint"
    constraint = spec.constraints[0]
    assert constraint({"url": "https://docs.python.org/3/"}) is None
    for bad in ["http://169.254.169.254/latest/", "http://127.0.0.1/", "file:///etc/passwd"]:
        verdict = constraint({"url": bad})
        assert verdict is not None and verdict[0] == "SANDBOX_VIOLATION", bad
