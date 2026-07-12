"""Documentation coverage checker (TITAN-WFL-034 machine half; Vol 6 §6.7).

Pure and hermetic: it takes the OpenAPI spec and the doc TEXT (API reference,
ADR index, README) and reports what §6.7 requires machine-checkably —
  * every public endpoint appears in the API reference WITH prose,
  * every ADR is indexed,
  * the README carries a Docker Compose quickstart.
The structured :class:`DocCoverageReport` is what gate G11 consumes later; the
prose-quality half (is the description meaningful?) stays with the reviewer.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

# A README quickstart must contain a Docker Compose up command (either spelling).
_COMPOSE_UP = re.compile(r"docker[\s-]compose\s+(?:-[^\n]*\s+)?up", re.IGNORECASE)


class EndpointCoverage(BaseModel):
    """Per-endpoint documentation status."""

    model_config = {"frozen": True}

    method: str
    path: str
    documented: bool
    has_prose: bool


class DocCoverageReport(BaseModel):
    """The structured §6.7 coverage result (G11 input)."""

    model_config = {"frozen": True}

    endpoints: tuple[EndpointCoverage, ...]
    missing_endpoints: tuple[str, ...]
    indexed_adrs: tuple[str, ...]
    missing_adrs: tuple[str, ...]
    readme_has_quickstart: bool

    @property
    def endpoint_coverage(self) -> float:
        if not self.endpoints:
            return 100.0
        documented = sum(1 for e in self.endpoints if e.documented and e.has_prose)
        return 100.0 * documented / len(self.endpoints)

    @property
    def ok(self) -> bool:
        """§6.7 acceptance: 100% endpoints (with prose), all ADRs indexed, a
        quickstart present."""
        return (
            not self.missing_endpoints
            and not self.missing_adrs
            and self.readme_has_quickstart
            and self.endpoint_coverage == 100.0
        )


def _iter_operations(openapi: dict[str, Any]) -> list[tuple[str, str]]:
    ops: list[tuple[str, str]] = []
    for path, item in (openapi.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            if method in item:
                ops.append((method.upper(), str(path)))
    return ops


def _path_on_line(line: str, path: str) -> bool:
    """Whether ``path`` appears on ``line`` as a WHOLE path, not as a prefix of a
    longer one. ``/widgets`` is a substring of ``/widgets/{id}``; matching on the
    substring would let the collection endpoint's heading falsely document the
    item endpoint (and vice versa). The path must not be followed by another path
    character (``/`` or an identifier / ``{``)."""
    for match in re.finditer(re.escape(path), line):
        nxt = line[match.end() : match.end() + 1]
        if nxt == "" or not (nxt.isalnum() or nxt in "/_{-"):
            return True
    return False


def _documented_with_prose(reference: str, method: str, path: str) -> bool:
    """Whether the reference documents this METHOD+PATH with prose.

    Method-aware on purpose: ``GET /widgets`` and ``POST /widgets`` share a path,
    so keying on the path alone would count one heading as documenting both. The
    operation is documented when a line names both the method and the path (as a
    whole path), and a following non-heading line carries real prose (a bare
    endpoint list does not count)."""
    lines = reference.splitlines()
    for index, line in enumerate(lines):
        if _path_on_line(line, path) and method.upper() in line.upper():
            for follow in lines[index + 1 : index + 6]:
                stripped = follow.strip()
                if not stripped or stripped.startswith(("#", "```", "|", "-", "*")):
                    continue
                if len(stripped.split()) >= 3:
                    return True
    return False


def check_doc_coverage(
    *,
    openapi: dict[str, Any],
    api_reference: str,
    readme: str,
    adr_ids: tuple[str, ...] = (),
    adr_index: str = "",
) -> DocCoverageReport:
    """Compute the §6.7 documentation coverage report. Pure."""
    endpoints: list[EndpointCoverage] = []
    missing: list[str] = []
    for method, path in _iter_operations(openapi):
        documented = any(_path_on_line(line, path) for line in api_reference.splitlines())
        has_prose = _documented_with_prose(api_reference, method, path)
        endpoints.append(
            EndpointCoverage(method=method, path=path, documented=documented, has_prose=has_prose)
        )
        if not has_prose:
            missing.append(f"{method} {path}")

    indexed = [adr for adr in adr_ids if adr in adr_index]
    missing_adrs = [adr for adr in adr_ids if adr not in adr_index]

    return DocCoverageReport(
        endpoints=tuple(endpoints),
        missing_endpoints=tuple(missing),
        indexed_adrs=tuple(indexed),
        missing_adrs=tuple(missing_adrs),
        readme_has_quickstart=bool(_COMPOSE_UP.search(readme)),
    )


__all__ = ["DocCoverageReport", "EndpointCoverage", "check_doc_coverage"]
