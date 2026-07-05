"""OpenAPI post-processing: error responses are problem+json (§4.1).

FastAPI documents validation failures as its own 422 shape; TITAN's contract
is RFC 9457 everywhere, so the published document (and therefore the
generated TypeScript client) must say so.
"""

from typing import Any

PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Problem",
    "description": "RFC 9457 Problem Details (§4.1 error contract)",
    "properties": {
        "type": {"type": "string", "default": "about:blank"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["title", "status"],
}


def problemify_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace non-2xx response bodies with the Problem schema in-place."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components["Problem"] = PROBLEM_SCHEMA
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)

    problem_content = {
        "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
    }
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for status_code, response in operation.get("responses", {}).items():
                if not (status_code.isdigit() and int(status_code) >= 400):
                    continue
                # Only rewrite the framework-generated shapes (FastAPI's 422
                # validation schema) and undocumented error responses. A route
                # that explicitly documents its own error model (e.g. readyz's
                # 503 ReadyzResponse) keeps it — the document must never lie
                # about what the handler returns.
                content = response.get("content")
                ref = (
                    content.get("application/json", {}).get("schema", {}).get("$ref", "")
                    if content
                    else ""
                )
                if content is None or ref.endswith("HTTPValidationError"):
                    response["content"] = problem_content
    return schema
