"""Agent-dependency gate (Phase 74, Vol 8 §11.3, TITAN-SEC-104/105).

The ONE enforcement + audit point for agent-proposed package installs: every
``pip``/``npm`` install inside the sandbox is pointed at the Phase-23 caching
mirror, and the mirror consults this gate on the FIRST contact with a package
(its index/packument) BEFORE any upstream fetch. A package is blocked unless:

  (a) it EXISTS on the official registry — kills hallucinated packages;
  (b) it is aged >= MIN_AGE_DAYS AND has >= MIN_WEEKLY_DOWNLOADS, OR is
      Maintainer-allowlisted — new/obscure packages need a human;
  (c) OSV shows no HIGH/CRITICAL vulnerability;
  (d) its name is NOT within edit-distance 1 of a well-known package unless it IS
      that package — kills typosquats.

Version pinning + hash-locking (SEC-104(e)) is enforced client-side by the
sandbox's pip/npm config (``--require-hashes`` / lockfile), not the mirror, which
only sees the name at index time; this module owns the name/metadata checks.

Pure decision logic lives in :func:`evaluate` (no I/O — fully unit-tested); the
network metadata + OSV lookups live behind :class:`MetadataResolver` (injected,
so tests drive recorded metadata).
"""

from __future__ import annotations

import datetime
import enum
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

MIN_AGE_DAYS = 30
MIN_WEEKLY_DOWNLOADS = 1000

# A curated set of high-profile packages typosquats imitate. NOT exhaustive — the
# real defense is (a)+(b)+(c); this catches the classic edit-distance-1 lure
# (e.g. `requsts`, `python-dateutil` vs `python-dateutil`, `beautifulsoup`).
TOP_PYPI: frozenset[str] = frozenset(
    {
        "requests",
        "urllib3",
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "pillow",
        "flask",
        "django",
        "fastapi",
        "starlette",
        "pydantic",
        "sqlalchemy",
        "alembic",
        "boto3",
        "botocore",
        "s3transfer",
        "click",
        "jinja2",
        "werkzeug",
        "markupsafe",
        "cryptography",
        "pyopenssl",
        "certifi",
        "charset-normalizer",
        "idna",
        "six",
        "python-dateutil",
        "pytz",
        "setuptools",
        "wheel",
        "pip",
        "packaging",
        "attrs",
        "pyyaml",
        "colorama",
        "tqdm",
        "rich",
        "typer",
        "httpx",
        "aiohttp",
        "anyio",
        "redis",
        "celery",
        "psycopg2",
        "psycopg",
        "asyncpg",
        "pymongo",
        "openai",
        "anthropic",
        "tensorflow",
        "torch",
        "scikit-learn",
        "transformers",
        "pytest",
        "coverage",
        "mypy",
        "ruff",
        "black",
        "flake8",
        "isort",
        "tox",
        "virtualenv",
        "poetry",
        "beautifulsoup4",
        "lxml",
        "selenium",
        "scrapy",
        "google-cloud-storage",
        "protobuf",
        "grpcio",
        "greenlet",
        "uvicorn",
        "gunicorn",
        "python-dotenv",
        "jsonschema",
        "markdown",
        "docutils",
        "sphinx",
        "opencv-python",
        "seaborn",
    }
)
TOP_NPM: frozenset[str] = frozenset(
    {
        "react",
        "react-dom",
        "lodash",
        "axios",
        "express",
        "chalk",
        "commander",
        "debug",
        "moment",
        "webpack",
        "babel-core",
        "typescript",
        "eslint",
        "prettier",
        "jest",
        "mocha",
        "chai",
        "vue",
        "angular",
        "next",
        "svelte",
        "vite",
        "rollup",
        "tailwindcss",
        "postcss",
        "autoprefixer",
        "dotenv",
        "cors",
        "body-parser",
        "mongoose",
        "sequelize",
        "pg",
        "mysql",
        "redis",
        "socket.io",
        "ws",
        "uuid",
        "jsonwebtoken",
        "bcrypt",
        "passport",
        "nodemon",
        "ts-node",
        "rimraf",
        "glob",
        "minimist",
        "yargs",
        "inquirer",
        "ora",
        "chokidar",
        "fs-extra",
        "cross-env",
        "classnames",
        "styled-components",
        "redux",
        "react-redux",
        "zustand",
        "immer",
        "graphql",
        "apollo-server",
        "zod",
        "yup",
        "date-fns",
        "dayjs",
        "ramda",
        "rxjs",
        "node-fetch",
        "form-data",
        "cheerio",
        "puppeteer",
        "playwright",
        "esbuild",
    }
)


class Reason(enum.StrEnum):
    ALLOWED = "allowed"
    ALLOWLISTED = "allowlisted"
    NONEXISTENT = "nonexistent"  # hallucinated
    TYPOSQUAT = "typosquat"
    INSUFFICIENT_ADOPTION = "insufficient_adoption"  # too new / too few downloads
    VULNERABLE = "vulnerable"  # OSV high/critical
    VULN_SCAN_UNAVAILABLE = "vuln_scan_unavailable"  # OSV lookup failed → fail closed


@dataclass(frozen=True)
class PackageMeta:
    """Registry facts about a candidate package (resolved out-of-band).

    ``vuln_scan_ok`` records whether the OSV lookup actually SUCCEEDED. A failed
    scan (OSV unreachable / timeout / non-200) must NOT be read as "no vuln known"
    — the gate fails closed on it (blocks), just like an unknown existence blocks.
    Defaults True so callers that construct fully-known metadata stay terse."""

    exists: bool
    age_days: int | None
    weekly_downloads: int | None
    has_high_or_critical_vuln: bool
    vuln_scan_ok: bool = True


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: Reason
    detail: str
    typosquat_of: str | None = None


def _within_edit_distance_1(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` differ by at most one insert/delete/substitution.
    O(len) with an early length-gap exit — no full DP table needed for the '<=1'
    question."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:  # at most one substitution
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    # lengths differ by exactly one: b must be a with one char inserted
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = 0
    edited = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        elif edited:
            return False
        else:
            edited = True
            j += 1  # skip the inserted char in the longer string
    return True


def typosquat_target(name: str, top: Iterable[str]) -> str | None:
    """The well-known package ``name`` squats (edit distance 1 but NOT equal), or
    None. An exact match is legitimate (the package IS the top one)."""
    lname = name.lower()
    for pkg in top:
        if lname != pkg and _within_edit_distance_1(lname, pkg):
            return pkg
    return None


def evaluate(
    name: str,
    meta: PackageMeta,
    *,
    top: Iterable[str],
    allowlisted: bool = False,
) -> GateDecision:
    """The pure SEC-104 decision. Order matters: a NONEXISTENT or TYPOSQUAT name is
    blocked even if allowlisting was requested for a *different* real package —
    but an explicit Maintainer allowlist entry for THIS name bypasses the
    age/adoption bar (b) ONLY — never existence, typosquat, a live vuln, or a
    failed (unavailable) vulnerability scan, all of which precede the allowlist."""
    if not meta.exists:
        return GateDecision(False, Reason.NONEXISTENT, f"'{name}' does not exist on the registry")
    squats = typosquat_target(name, top)
    if squats is not None:
        return GateDecision(
            False, Reason.TYPOSQUAT, f"'{name}' resembles '{squats}' (possible typosquat)", squats
        )
    if meta.has_high_or_critical_vuln:
        return GateDecision(False, Reason.VULNERABLE, f"'{name}' has a high/critical OSV advisory")
    if not meta.vuln_scan_ok:
        # OSV was unreachable — fail closed on the vuln dimension (do NOT let an
        # allowlist entry bypass this; a scan outage must not silently allow).
        return GateDecision(
            False,
            Reason.VULN_SCAN_UNAVAILABLE,
            f"'{name}' could not be vulnerability-scanned (OSV unavailable); failing closed",
        )
    if allowlisted:
        return GateDecision(True, Reason.ALLOWLISTED, f"'{name}' is Maintainer-allowlisted")
    aged = meta.age_days is not None and meta.age_days >= MIN_AGE_DAYS
    adopted = meta.weekly_downloads is not None and meta.weekly_downloads >= MIN_WEEKLY_DOWNLOADS
    if not (aged and adopted):
        return GateDecision(
            False,
            Reason.INSUFFICIENT_ADOPTION,
            f"'{name}' is too new or low-adoption "
            f"(age={meta.age_days}d, weekly_downloads={meta.weekly_downloads}); "
            "needs age>=30d + >=1000 weekly downloads or a Maintainer allowlist",
        )
    return GateDecision(True, Reason.ALLOWED, f"'{name}' passed the dependency gate")


# --- request parsing: which mirror requests are a first-contact package gate ---


def normalize_name(raw: str) -> str:
    """PEP 503 normalization (lowercase, runs of ``-_.`` -> a single ``-``). Applied
    to BOTH request names and Maintainer-allowlist entries so a config entry like
    ``My_Internal_Pkg`` still matches the normalized request name (SEC-104 review)."""
    return re.sub(r"[-_.]+", "-", raw).lower()


def parse_pypi_name(path: str) -> str | None:
    """The package name from a pip SIMPLE-index request (``simple/{name}/``), or
    None for a non-index path. The index request is the primary first-contact gate;
    direct artifact downloads are covered by :func:`parse_pypi_artifact_name`."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "simple":
        return normalize_name(parts[1])
    return None


def parse_pypi_artifact_name(path: str) -> str | None:
    """The package name from a DIRECT pip artifact download
    (``packages/**/{name}-{version}...whl|.tar.gz|.zip``), or None. Gating this too
    closes the bypass where a malicious agent fetches the wheel/sdist URL directly,
    skipping the SIMPLE index (SEC-104 review). A mis-parse fails CLOSED: the
    resolver reports the odd name NONEXISTENT and the gate blocks."""
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts or parts[0] != "packages":
        return None
    fname = parts[-1]
    for suffix in (".whl", ".tar.gz", ".tar.bz2", ".zip", ".egg"):
        if fname.endswith(suffix):
            stem = fname[: -len(suffix)]
            break
    else:
        return None
    # Wheels + sdists are ``{name}-{version}...``; the name is everything before
    # the first ``-`` that introduces a version (a leading digit).
    m = re.match(r"^(?P<name>.+?)-(?P<ver>\d[^-]*)", stem)
    if not m:
        return None
    return normalize_name(m.group("name"))


def parse_npm_name(path: str) -> str | None:
    """The package name from an npm packument request (``{name}`` or
    ``@scope/{name}``), or None for a tarball download (handled by
    :func:`parse_npm_artifact_name`)."""
    p = path.strip("/")
    if not p or "/-/" in p or p.endswith(".tgz"):
        return None
    parts = p.split("/")
    if p.startswith("@"):  # scoped: @scope/name
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def parse_npm_artifact_name(path: str) -> str | None:
    """The package name from a DIRECT npm tarball download
    (``{name}/-/{file}.tgz`` or ``@scope/{name}/-/{file}.tgz``), or None. npm serves
    tarballs from the packument host, so a malicious agent can ``npm install
    <tarball-url>`` (running postinstall) without ever hitting the packument — the
    name is the reliable path segment BEFORE ``/-/``."""
    p = path.strip("/")
    if "/-/" not in p:
        return None
    before = p.split("/-/", 1)[0]
    if not before:
        return None
    parts = before.split("/")
    if before.startswith("@"):  # scoped: @scope/name/-/...
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def _osv_ecosystem(ecosystem: str) -> str:
    return "PyPI" if ecosystem == "pypi" else "npm"


class MetadataResolver:
    """Resolves the registry facts the gate needs, best-effort, over an injected
    httpx client (so tests drive recorded responses). Network failures degrade to
    ``exists=False`` / no-vuln-known conservatively — a resolver outage must not
    silently ALLOW an unknown package, so unknown existence blocks (fail-closed on
    the hallucination check), while an OSV/adoption outage is treated as
    unknown (the age/adoption bar then blocks anyway unless allowlisted)."""

    def __init__(self, client: httpx.AsyncClient, *, timeout: float = 10.0) -> None:
        self._client = client
        self._timeout = timeout

    async def resolve(self, ecosystem: str, name: str) -> PackageMeta:
        if ecosystem == "pypi":
            return await self._resolve_pypi(name)
        return await self._resolve_npm(name)

    async def _get_json(self, url: str) -> tuple[int, dict[str, Any] | None]:
        try:
            r = await self._client.get(url, timeout=self._timeout, follow_redirects=True)
        except httpx.HTTPError:
            return 0, None
        if r.status_code != 200:
            return r.status_code, None
        try:
            body: dict[str, Any] = r.json()
        except Exception:
            return r.status_code, None
        return 200, body

    async def _resolve_pypi(self, name: str) -> PackageMeta:
        status, meta = await self._get_json(f"https://pypi.org/pypi/{name}/json")
        if status != 200 or meta is None:
            return PackageMeta(
                exists=False, age_days=None, weekly_downloads=None, has_high_or_critical_vuln=False
            )
        age = _earliest_release_age_days(meta.get("releases") or {})
        _, dl = await self._get_json(f"https://pypistats.org/api/packages/{name}/recent")
        weekly = ((dl or {}).get("data") or {}).get("last_week") if dl else None
        vuln, scan_ok = await self._osv_has_high("PyPI", name)
        return PackageMeta(True, age, weekly if isinstance(weekly, int) else None, vuln, scan_ok)

    async def _resolve_npm(self, name: str) -> PackageMeta:
        status, meta = await self._get_json(f"https://registry.npmjs.org/{name}")
        if status != 200 or meta is None:
            return PackageMeta(
                exists=False, age_days=None, weekly_downloads=None, has_high_or_critical_vuln=False
            )
        created = (meta.get("time") or {}).get("created")
        age = _age_days_from_iso(created) if isinstance(created, str) else None
        _, dl = await self._get_json(f"https://api.npmjs.org/downloads/point/last-week/{name}")
        weekly = (dl or {}).get("downloads") if dl else None
        vuln, scan_ok = await self._osv_has_high("npm", name)
        return PackageMeta(True, age, weekly if isinstance(weekly, int) else None, vuln, scan_ok)

    async def _osv_has_high(self, ecosystem: str, name: str) -> tuple[bool, bool]:
        """Returns ``(has_high_or_critical, scan_ok)``. ``scan_ok`` is False when
        OSV could not be reached/parsed — the caller then fails CLOSED rather than
        treating an outage as 'no vuln known'."""
        try:
            r = await self._client.post(
                "https://api.osv.dev/v1/query",
                json={"package": {"name": name, "ecosystem": ecosystem}},
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return False, False
        if r.status_code != 200:
            return False, False
        try:
            vulns = r.json().get("vulns") or []
        except Exception:
            return False, False
        return any(_vuln_is_high(v) for v in vulns), True


def _cvss_v3_base_score(vector: str) -> float | None:
    """The CVSS v3.0/v3.1 base score computed from a vector string, or None if the
    string is not a scoreable v3 vector. OSV's ``severity[].score`` for type
    CVSS_V3 is the VECTOR (e.g. 'CVSS:3.1/AV:N/AC:L/...'), not a number, so without
    this a HIGH advisory carrying only a vector would slip past the vuln gate."""
    if not vector.startswith("CVSS:3"):
        return None  # v4 / v2 / non-vector — fall back to database_specific severity
    metrics: dict[str, str] = {}
    for part in vector.split("/")[1:]:
        key, _, val = part.partition(":")
        if val:
            metrics[key] = val
    ci = {"H": 0.56, "L": 0.22, "N": 0.0}
    try:
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[metrics["AV"]]
        ac = {"L": 0.77, "H": 0.44}[metrics["AC"]]
        ui = {"N": 0.85, "R": 0.62}[metrics["UI"]]
        scope_changed = metrics["S"] == "C"
        pr = (
            {"N": 0.85, "L": 0.68, "H": 0.5} if scope_changed else {"N": 0.85, "L": 0.62, "H": 0.27}
        )[metrics["PR"]]
        conf, integ, avail = ci[metrics["C"]], ci[metrics["I"]], ci[metrics["A"]]
    except KeyError:
        return None
    iss = 1 - (1 - conf) * (1 - integ) * (1 - avail)
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    raw = min((impact + exploitability) * (1.08 if scope_changed else 1.0), 10.0)
    return math.ceil(raw * 10) / 10  # CVSS "roundup" to one decimal


def _vuln_is_high(vuln: dict[str, Any]) -> bool:
    """A vuln counts as high/critical if any CVSS base score >= 7.0 (from a bare
    numeric score OR a computed v3 vector), or a database_specific severity says
    HIGH/CRITICAL."""
    for sev in vuln.get("severity") or []:
        raw = str(sev.get("score", "")).strip()
        try:
            if float(raw) >= 7.0:  # a bare numeric base score
                return True
            continue
        except (TypeError, ValueError):
            pass
        score = _cvss_v3_base_score(raw)  # a CVSS v3 vector string
        if score is not None and score >= 7.0:
            return True
    label = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
    return label in {"HIGH", "CRITICAL"}


def _age_days_from_iso(iso: str) -> int | None:
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - dt).days


def _earliest_release_age_days(releases: dict[str, Any]) -> int | None:
    times: list[str] = []
    for files in releases.values():
        for f in files or []:
            t = f.get("upload_time_iso_8601") or f.get("upload_time")
            if isinstance(t, str):
                times.append(t)
    if not times:
        return None
    ages = [a for a in (_age_days_from_iso(t) for t in times) if a is not None]
    return max(ages) if ages else None  # the OLDEST release = the package's age


__all__ = [
    "MIN_AGE_DAYS",
    "MIN_WEEKLY_DOWNLOADS",
    "TOP_NPM",
    "TOP_PYPI",
    "GateDecision",
    "MetadataResolver",
    "PackageMeta",
    "Reason",
    "evaluate",
    "normalize_name",
    "parse_npm_artifact_name",
    "parse_npm_name",
    "parse_pypi_artifact_name",
    "parse_pypi_name",
    "typosquat_target",
]
