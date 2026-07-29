"""Phase 74 (Vol 8 §11.3): the SEC-104 agent-dependency gate at the Phase-23
mirror — the single enforcement point (SEC-105).

DoD: a hallucinated (nonexistent) package is rejected at the mirror while a
legitimate package passes; a typosquat is blocked. Pure decision logic +
parsing + the resolver (mocked httpx) + the gateway hook (no DB — the
install-decision sink is a recording fake). Compliance-map (TITAN-SEC-110) tokens
TITAN-SEC-104/105 are carried in the assertions below, not this docstring.
"""

from typing import Any, cast

import httpx
from fastapi.testclient import TestClient

from titan.tools.egress import dependency_gate as dg
from titan.tools.egress.cache import ResponseCache
from titan.tools.egress.dependency_gate import (
    GateDecision,
    MetadataResolver,
    PackageMeta,
    Reason,
    evaluate,
    parse_npm_artifact_name,
    parse_npm_name,
    parse_pypi_artifact_name,
    parse_pypi_name,
    typosquat_target,
)
from titan.tools.egress.gateway import build_gateway_app

_GOOD = PackageMeta(
    exists=True, age_days=400, weekly_downloads=1_000_000, has_high_or_critical_vuln=False
)


# --- pure decision logic ---------------------------------------------------


def test_edit_distance_1() -> None:
    assert dg._within_edit_distance_1("requests", "requests")  # equal
    assert dg._within_edit_distance_1("requsts", "requests")  # deletion
    assert dg._within_edit_distance_1("reqests", "requests")  # deletion
    assert dg._within_edit_distance_1("requestss", "requests")  # insertion
    assert dg._within_edit_distance_1("reuqests", "requests") is False  # transposition = 2 edits
    assert dg._within_edit_distance_1("flask", "django") is False


def test_typosquat_target() -> None:
    assert typosquat_target("requsts", dg.TOP_PYPI) == "requests"
    assert typosquat_target("requests", dg.TOP_PYPI) is None  # the real package
    assert typosquat_target("totally-novel-pkg", dg.TOP_PYPI) is None
    assert typosquat_target("expres", dg.TOP_NPM) == "express"


def test_evaluate_order_and_reasons() -> None:
    top = dg.TOP_PYPI
    # nonexistent beats everything (even a name that also looks like a typosquat).
    assert (
        evaluate("requsts", PackageMeta(False, None, None, False), top=top).reason
        == Reason.NONEXISTENT
    )
    # a typosquat that EXISTS is still blocked (attacker registered it).
    d = evaluate("requsts", PackageMeta(True, 400, 1_000_000, False), top=top)
    assert not d.allowed and d.reason == Reason.TYPOSQUAT and d.typosquat_of == "requests"
    # a live vuln blocks even a well-adopted, allowlisted package.
    assert (
        evaluate(
            "numpy", PackageMeta(True, 4000, 9_000_000, True), top=top, allowlisted=True
        ).reason
        == Reason.VULNERABLE
    )
    # too new / too few downloads blocks; an allowlist entry bypasses THAT bar.
    young = PackageMeta(True, 3, 5, False)
    assert evaluate("brand-new-lib", young, top=top).reason == Reason.INSUFFICIENT_ADOPTION
    assert evaluate("brand-new-lib", young, top=top, allowlisted=True).allowed
    # a real, aged, adopted, clean package passes.
    assert evaluate("some-mature-lib", _GOOD, top=top).allowed


def test_evaluate_fails_closed_when_vuln_scan_unavailable() -> None:
    # SEC-104 review #2: an OSV outage must not be read as "no vuln" — fail closed,
    # and NOT even an allowlist entry may bypass it.
    top = dg.TOP_PYPI
    meta = PackageMeta(True, 400, 1_000_000, False, vuln_scan_ok=False)
    assert evaluate("some-lib", meta, top=top).reason == Reason.VULN_SCAN_UNAVAILABLE
    assert evaluate("some-lib", meta, top=top, allowlisted=True).reason == (
        Reason.VULN_SCAN_UNAVAILABLE
    )


def test_cvss_v3_vector_scores_as_high() -> None:
    # SEC-104 review #1: OSV frequently carries only a CVSS vector string (no numeric
    # score, no database_specific.severity). A HIGH vector must score >= 7.0.
    high = {
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]
    }
    low = {
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N"}]
    }
    assert dg._vuln_is_high(high) is True
    assert dg._vuln_is_high(low) is False
    assert dg._cvss_v3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert dg._cvss_v3_base_score("not-a-vector") is None  # v4/v2 → fall back to db_specific


# --- request parsing -------------------------------------------------------


def test_parse_pypi_name() -> None:
    assert parse_pypi_name("simple/requests/") == "requests"
    assert parse_pypi_name("/simple/Flask/") == "flask"  # PEP 503 normalization
    assert parse_pypi_name("simple/zope.interface/") == "zope-interface"
    assert (
        parse_pypi_name("packages/ab/cd/requests-2.31.0-py3-none-any.whl") is None
    )  # file, not index
    assert parse_pypi_name("simple/") is None


def test_parse_npm_name() -> None:
    assert parse_npm_name("lodash") == "lodash"
    assert parse_npm_name("@types/node") == "@types/node"
    assert parse_npm_name("react/-/react-18.2.0.tgz") is None  # tarball, not packument
    assert parse_npm_name("@babel/core/-/core-7.0.0.tgz") is None
    assert parse_npm_name("") is None


# --- the metadata resolver (mocked registry APIs) --------------------------


def test_resolver_pypi_shapes() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://pypi.org/pypi/requests/json":
            return httpx.Response(
                200,
                json={"releases": {"2.0": [{"upload_time_iso_8601": "2013-01-01T00:00:00Z"}]}},
            )
        if url == "https://pypistats.org/api/packages/requests/recent":
            return httpx.Response(200, json={"data": {"last_week": 5_000_000}})
        if "osv.dev" in url:
            return httpx.Response(200, json={"vulns": []})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = MetadataResolver(client)
    meta = asyncio.run(resolver.resolve("pypi", "requests"))
    assert meta.exists and meta.weekly_downloads == 5_000_000
    assert meta.age_days is not None and meta.age_days > 3000
    assert meta.has_high_or_critical_vuln is False
    # A nonexistent package: the JSON endpoint 404s -> exists=False.
    assert asyncio.run(resolver.resolve("pypi", "does-not-exist")).exists is False
    asyncio.run(client.aclose())


def test_resolver_flags_high_osv() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        if "registry.npmjs.org" in str(request.url):
            return httpx.Response(200, json={"time": {"created": "2015-01-01T00:00:00Z"}})
        if "api.npmjs.org" in str(request.url):
            return httpx.Response(200, json={"downloads": 2_000_000})
        if "osv.dev" in str(request.url):
            return httpx.Response(
                200, json={"vulns": [{"database_specific": {"severity": "CRITICAL"}}]}
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = asyncio.run(MetadataResolver(client).resolve("npm", "somepkg"))
    assert meta.exists and meta.has_high_or_critical_vuln is True
    assert meta.vuln_scan_ok is True
    asyncio.run(client.aclose())


def test_resolver_osv_outage_fails_closed() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://pypi.org/pypi/somepkg/json":
            return httpx.Response(
                200,
                json={"releases": {"1.0": [{"upload_time_iso_8601": "2013-01-01T00:00:00Z"}]}},
            )
        if "pypistats.org" in url:
            return httpx.Response(200, json={"data": {"last_week": 5_000_000}})
        if "osv.dev" in url:
            return httpx.Response(500)  # OSV down
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = asyncio.run(MetadataResolver(client).resolve("pypi", "somepkg"))
    assert meta.exists and meta.vuln_scan_ok is False
    # a well-adopted package is now BLOCKED (fail-closed), not allowed.
    assert evaluate("somepkg", meta, top=dg.TOP_PYPI).reason == Reason.VULN_SCAN_UNAVAILABLE
    asyncio.run(client.aclose())


# --- the gateway hook (the DoD end-to-end) ---------------------------------


class _FakeResolver:
    def __init__(self, table: dict[str, PackageMeta]) -> None:
        self._table = table

    async def resolve(self, ecosystem: str, name: str) -> PackageMeta:
        return self._table.get(name, PackageMeta(False, None, None, False))


def _gateway_client(
    resolver: Any, *, allowlist: frozenset[str] = frozenset()
) -> tuple[TestClient, list[Any]]:
    recorded: list[Any] = []

    async def on_decision(name: str, ecosystem: str, decision: GateDecision) -> None:
        recorded.append((name, ecosystem, decision.reason))

    # A MockTransport that serves any upstream index as 200 (the pull-through).
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="INDEX"))
    )
    app = build_gateway_app(
        client=upstream,
        cache=ResponseCache(),
        search_backend=None,
        internal_token="tok",
        pypi_upstream="https://pypi.org",
        npm_upstream="https://registry.npmjs.org",
        dependency_resolver=cast(MetadataResolver, resolver),
        dependency_allowlist=allowlist,
        on_install_decision=on_decision,
    )
    return TestClient(app), recorded


def test_mirror_blocks_hallucinated_and_typosquat_but_passes_legit() -> None:
    resolver = _FakeResolver({"requests": _GOOD})  # only 'requests' exists+good
    client, recorded = _gateway_client(resolver)

    # Legit, aged, adopted package → 200 pull-through.
    ok = client.get("/pypi/simple/requests/")
    assert ok.status_code == 200 and ok.text == "INDEX"

    # Hallucinated (not in the resolver table → exists=False) → 403 at the mirror.
    hallucinated = client.get("/pypi/simple/totally-made-up-pkg-xyz/")
    assert hallucinated.status_code == 403, "TITAN-SEC-104: a hallucinated package must be blocked"
    assert "TITAN-SEC-104" in hallucinated.json()["detail"]

    # Typosquat of 'requests' that also "exists" → 403.
    resolver2 = _FakeResolver({"requsts": _GOOD})
    client2, _ = _gateway_client(resolver2)
    squat = client2.get("/pypi/simple/requsts/")
    assert squat.status_code == 403 and "typosquat" in squat.json()["detail"].lower()

    # Every decision was recorded to the install-decision sink (allow + blocks).
    reasons = {r[0]: r[2] for r in recorded}
    assert reasons["requests"] == Reason.ALLOWED
    assert reasons["totally-made-up-pkg-xyz"] == Reason.NONEXISTENT


def test_mirror_gates_direct_artifact_downloads() -> None:
    # SEC-104 review #3: a malicious agent must not bypass the gate by fetching the
    # wheel/tarball URL directly instead of the index. The name is parsed from the
    # artifact filename and gated exactly like an index request.
    resolver = _FakeResolver({"requests": _GOOD})  # only 'requests' exists+good
    client, recorded = _gateway_client(resolver)

    # A hallucinated wheel (name parsed from the filename → exists=False) → 403.
    bad_wheel = client.get("/pypi/packages/ab/cd/totally-made-up-xyz-1.0.0-py3-none-any.whl")
    assert bad_wheel.status_code == 403, "TITAN-SEC-105: direct artifact fetches are gated too"
    assert recorded[-1][2] == Reason.NONEXISTENT

    # A hallucinated npm tarball (name before /-/ → exists=False) → 403.
    bad_tgz = client.get("/npm/made-up-pkg/-/made-up-pkg-1.0.0.tgz")
    assert bad_tgz.status_code == 403

    # A legit package's wheel still serves (existence/adoption pass).
    good_wheel = client.get("/pypi/packages/ff/00/requests-2.31.0-py3-none-any.whl")
    assert good_wheel.status_code == 200


def test_parse_artifact_names() -> None:
    assert parse_pypi_artifact_name("packages/ab/cd/requests-2.31.0-py3-none-any.whl") == "requests"
    assert parse_pypi_artifact_name("packages/xx/yy/zope.interface-6.0.tar.gz") == "zope-interface"
    assert parse_pypi_artifact_name("packages/x/y/typing_extensions-4.9.0-py3-none-any.whl") == (
        "typing-extensions"
    )
    assert parse_pypi_artifact_name("simple/requests/") is None  # an index, not an artifact
    assert parse_npm_artifact_name("react/-/react-18.2.0.tgz") == "react"
    assert parse_npm_artifact_name("@babel/core/-/core-7.0.0.tgz") == "@babel/core"
    assert parse_npm_artifact_name("lodash") is None  # a packument, not a tarball


def test_allowlist_bypasses_the_adoption_bar_at_the_mirror() -> None:
    young = PackageMeta(
        exists=True, age_days=2, weekly_downloads=3, has_high_or_critical_vuln=False
    )
    resolver = _FakeResolver({"internal-lib": young})
    # Without allowlist → blocked; with allowlist → allowed.
    blocked, _ = _gateway_client(resolver)
    assert blocked.get("/pypi/simple/internal-lib/").status_code == 403
    allowed, _ = _gateway_client(resolver, allowlist=frozenset({"internal-lib"}))
    assert allowed.get("/pypi/simple/internal-lib/").status_code == 200


def test_allowlist_matches_non_normalized_config_entries() -> None:
    # SEC-104 review #11: a config entry `Internal_Lib` must still match the
    # PEP-503-normalized request name `internal-lib` (both normalized before compare).
    young = PackageMeta(
        exists=True, age_days=2, weekly_downloads=3, has_high_or_critical_vuln=False
    )
    resolver = _FakeResolver({"internal-lib": young})
    allowed, _ = _gateway_client(resolver, allowlist=frozenset({"Internal_Lib"}))
    assert allowed.get("/pypi/simple/internal_lib/").status_code == 200


def test_gate_off_when_no_resolver() -> None:
    client, recorded = _gateway_client(None)
    assert client.get("/pypi/simple/anything/").status_code == 200
    assert recorded == []


pytestmark: list[Any] = []  # no DB — pure + httpx-mocked
