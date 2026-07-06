"""Pure unit tests for the egress building blocks (Phase 23) — no Docker, no
network, no DB. Covers the per-task token, the allowlist, the response cache, and
readability->markdown extraction."""

import uuid

from titan.tools.egress.allowlist import REGISTRY_HOSTS, Allowlist
from titan.tools.egress.cache import CachedResponse, ResponseCache
from titan.tools.egress.tokens import TokenClaims, mint_task_token, verify_task_token
from titan.tools.web.extract import html_to_markdown


def _claims(**over: object) -> TokenClaims:
    base = dict(
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        agent_id="sandbox",
        git_host=None,
        approved_hosts=(),
        expires_at=0.0,
    )
    base.update(over)
    return TokenClaims(**base)  # type: ignore[arg-type]


# --- tokens ------------------------------------------------------------------


def test_token_roundtrip_and_tamper_detection() -> None:
    project, task = uuid.uuid4(), uuid.uuid4()
    token = mint_task_token(
        "s3cret", project_id=project, task_id=task, agent_id="sandbox", git_host="github.com"
    )
    claims = verify_task_token("s3cret", token)
    assert claims is not None
    assert claims.project_id == project and claims.task_id == task
    assert claims.git_host == "github.com"
    # Wrong secret, tampered payload, and garbage all reject.
    assert verify_task_token("wrong", token) is None
    payload, sig = token.split(".", 1)
    assert verify_task_token("s3cret", f"{payload}x.{sig}") is None
    assert verify_task_token("s3cret", "not-a-token") is None


def test_token_expiry() -> None:
    token = mint_task_token(
        "s", project_id=uuid.uuid4(), task_id=None, agent_id="sandbox", ttl_seconds=10, now=1000.0
    )
    assert verify_task_token("s", token, now=1005.0) is not None  # still valid
    assert verify_task_token("s", token, now=1011.0) is None  # expired


# --- allowlist ---------------------------------------------------------------


def test_allowlist_registry_git_and_approved() -> None:
    allow = Allowlist(mirror_hosts=frozenset({"titan-egress"}))
    claims = _claims(git_host="gitlab.example", approved_hosts=("docs.example",))
    assert "pypi.org" in REGISTRY_HOSTS
    assert allow.is_allowed("pypi.org", claims)  # registry
    assert allow.is_allowed("files.pythonhosted.org", claims)
    assert allow.is_allowed("titan-egress", claims)  # mirror host
    assert allow.is_allowed("gitlab.example", claims)  # task git remote
    assert allow.is_allowed("docs.example", claims)  # admin-approved (token)
    assert not allow.is_allowed("example.com", claims)  # denied
    assert not allow.is_allowed("evil.test", claims)


def test_allowlist_case_and_trailing_dot() -> None:
    allow = Allowlist()
    claims = _claims(git_host="GitHub.com")
    assert allow.is_allowed("PYPI.ORG", claims)
    assert allow.is_allowed("github.com.", claims)  # trailing-dot FQDN


# --- cache -------------------------------------------------------------------


def test_cache_hit_miss_ttl_and_lru() -> None:
    cache = ResponseCache(max_entries=2, ttl_seconds=100.0)
    resp = CachedResponse(status=200, content_type="text/html", body=b"x", final_url="u")
    assert cache.get("a", now=0.0) is None and cache.misses == 1
    cache.put("a", resp, now=0.0)
    assert cache.get("a", now=1.0) is not None and cache.hits == 1
    assert cache.get("a", now=101.0) is None  # TTL expired
    # LRU eviction at capacity 2.
    cache.put("b", resp, now=200.0)
    cache.put("c", resp, now=200.0)
    cache.put("d", resp, now=200.0)  # evicts the LRU ("b")
    assert cache.get("b", now=200.0) is None
    assert cache.get("d", now=200.0) is not None


# --- extraction --------------------------------------------------------------

_ARTICLE = """<html><head><title>Titan Guide</title></head><body>
<nav>Home About Contact</nav>
<article><h1>Getting Started</h1>
<p>Install with <a href="https://x/y">the CLI</a> to begin building on the
platform. The command-line tool bootstraps a workspace, wires the services, and
gives you a running stack within a couple of minutes on any machine.</p>
<pre><code>titan up</code></pre>
<p>Once the stack is running you can create your first project, invite agents,
and start assigning tasks. The dashboard shows live progress, streamed logs, and
the full audit trail of every side effect an agent performs on your behalf.</p>
<p>From there the platform handles orchestration, review, and deployment so you
can focus on the product rather than the plumbing that normally surrounds it.</p>
</article>
<footer>Copyright 2026 secret-footer-text</footer></body></html>"""


def test_extract_returns_markdown_not_raw_html() -> None:
    result = html_to_markdown(_ARTICLE, url="https://x.test/")
    md = result["markdown"]
    assert result["title"]
    # Markdown markup is present (heading, link), not raw HTML tags.
    assert "# Getting Started" in md
    assert "[the CLI](https://x/y)" in md
    assert "<article>" not in md and "<h1>" not in md
    # Boilerplate stripped.
    assert "secret-footer-text" not in md


def test_extract_degenerate_html_still_returns_something() -> None:
    result = html_to_markdown("<p>just a tiny snippet of text</p>")
    assert "tiny snippet" in result["markdown"]
