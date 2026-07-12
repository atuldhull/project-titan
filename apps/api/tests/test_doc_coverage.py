"""The §6.7 documentation coverage checker (Phase 47, TITAN-WFL-034). Hermetic."""

from typing import Any

from titan.docs.doc_coverage import check_doc_coverage

_OPENAPI: dict[str, Any] = {
    "paths": {
        "/api/v1/widgets": {"get": {}, "post": {}},
        "/api/v1/widgets/{widget_id}": {"get": {}},
    }
}

_FULL_REFERENCE = """
# API Reference

## GET /api/v1/widgets
Returns the paginated list of widgets visible to the caller.

## POST /api/v1/widgets
Creates a new widget from the supplied attributes and returns it.

## GET /api/v1/widgets/{widget_id}
Returns a single widget by id, or a 404 problem-details when absent.
"""

_README = """
# My Service

## Quickstart

```bash
docker compose up -d
```

Then open http://localhost:8000/healthz to confirm the service is live.
"""


def test_full_docs_report_100_percent_and_ok() -> None:
    report = check_doc_coverage(
        openapi=_OPENAPI,
        api_reference=_FULL_REFERENCE,
        readme=_README,
        adr_ids=("ADR-001", "ADR-002"),
        adr_index="- ADR-001 — layering\n- ADR-002 — auth\n",
    )
    assert report.ok
    assert report.endpoint_coverage == 100.0
    assert not report.missing_endpoints
    assert not report.missing_adrs
    assert report.readme_has_quickstart


def test_missing_endpoint_prose_is_flagged() -> None:
    # The reference lists the endpoints but with no prose (a bare list).
    bare = "\n".join(
        f"- {m} {p}"
        for p in _OPENAPI["paths"]
        for m in ("GET",)  # noqa: F841 - illustrative
    )
    report = check_doc_coverage(openapi=_OPENAPI, api_reference=bare, readme=_README)
    assert not report.ok
    assert report.missing_endpoints
    assert report.endpoint_coverage < 100.0


def test_undocumented_endpoint_is_missing() -> None:
    reference = "## GET /api/v1/widgets\nLists widgets in full prose sentences here.\n"
    report = check_doc_coverage(openapi=_OPENAPI, api_reference=reference, readme=_README)
    assert "POST /api/v1/widgets" in report.missing_endpoints
    assert "GET /api/v1/widgets/{widget_id}" in report.missing_endpoints


def test_item_endpoint_docs_do_not_satisfy_the_collection_endpoint() -> None:
    """Path matching is whole-path: documenting `/api/v1/widgets/{widget_id}` must
    NOT be read as documenting the shorter `/api/v1/widgets` (substring), or an
    undocumented endpoint would falsely pass the §6.7 gate."""
    reference = (
        "## GET /api/v1/widgets/{widget_id}\n"
        "Returns a single widget by id with full prose describing the response.\n"
    )
    report = check_doc_coverage(openapi=_OPENAPI, api_reference=reference, readme=_README)
    # The collection GET/POST are NOT documented by the item endpoint's heading.
    assert "GET /api/v1/widgets" in report.missing_endpoints
    assert "POST /api/v1/widgets" in report.missing_endpoints
    # ...but the item endpoint itself IS documented.
    assert "GET /api/v1/widgets/{widget_id}" not in report.missing_endpoints


def test_unindexed_adr_is_reported() -> None:
    report = check_doc_coverage(
        openapi=_OPENAPI,
        api_reference=_FULL_REFERENCE,
        readme=_README,
        adr_ids=("ADR-001", "ADR-002"),
        adr_index="- ADR-001 — layering\n",  # ADR-002 not indexed
    )
    assert report.missing_adrs == ("ADR-002",)
    assert not report.ok


def test_readme_without_quickstart_is_flagged() -> None:
    report = check_doc_coverage(
        openapi=_OPENAPI,
        api_reference=_FULL_REFERENCE,
        readme="# My Service\n\nNo quickstart here.\n",
    )
    assert not report.readme_has_quickstart
    assert not report.ok


def test_docker_compose_hyphen_spelling_is_accepted() -> None:
    report = check_doc_coverage(
        openapi={"paths": {}},
        api_reference="",
        readme="Run `docker-compose up` to start.\n",
    )
    assert report.readme_has_quickstart
