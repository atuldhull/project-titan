"""The first-party Figma READ connector (Phase 82; Vol 7 §6.5, TITAN-INT-044/011).

In-process against recorded fixtures (no network): each read tool's request + auth header, the
soft-error envelope, and the design-variables → tailwind.config.ts extraction (INT-044 DoD).
Skips cleanly if the connector directory is absent (INT-011)."""

import json
from typing import Any

from tests.connector_loader import load_connector

fg_http: Any = load_connector("figma", "figma_http")
fg_api: Any = load_connector("figma", "figma_api")
fg_tokens: Any = load_connector("figma", "figma_tokens")
fg_plugin: Any = load_connector("figma", "figma_plugin")


def _resp(status: int, body: dict[str, Any], **headers: str) -> Any:
    return fg_http.HttpResponse(status, dict(headers), json.dumps(body))


def _connector(transport: Any, config: dict[str, str] | None = None) -> Any:
    return fg_plugin.FigmaConnector(
        None,
        http=transport,
        config=config or {"FIGMA_TOKEN": "figtok", "FIGMA_API_URL": "https://api.figma.com"},
    )


# --- tool request building + auth --------------------------------------------


def test_get_file_uses_pat_header_and_path() -> None:
    tx = fg_http.RecordedTransport().add("GET", "/v1/files/ABC", _resp(200, {"document": {}}))
    out = _connector(tx).call_tool("figma.get_file", {"file_key": "ABC", "depth": 2})
    assert out["ok"] is True
    method, url, headers, _ = tx.calls[0]
    assert method == "GET" and "/v1/files/ABC" in url and "depth=2" in url
    assert headers["x-figma-token"] == "figtok" and "authorization" not in headers


def test_get_node_image_builds_ids_and_format() -> None:
    tx = fg_http.RecordedTransport().add("GET", "/v1/images/ABC", _resp(200, {"images": {}}))
    _connector(tx).call_tool(
        "figma.get_node_image", {"file_key": "ABC", "ids": ["1:2", "3:4"], "scale": 2}
    )
    _, url, _, _ = tx.calls[0]
    assert "/v1/images/ABC" in url and "ids=1%3A2%2C3%3A4" in url and "format=png" in url


def test_get_variables_and_comments_paths() -> None:
    tx = (
        fg_http.RecordedTransport()
        .add("GET", "/variables/local", _resp(200, {"meta": {}}))
        .add("GET", "/comments", _resp(200, {"comments": []}))
    )
    conn = _connector(tx)
    assert conn.call_tool("figma.get_variables", {"file_key": "ABC"})["ok"]
    assert conn.call_tool("figma.get_comments", {"file_key": "ABC"})["ok"]
    assert "/v1/files/ABC/variables/local" in tx.calls[0][1]
    assert "/v1/files/ABC/comments" in tx.calls[1][1]


def test_oauth_bearer_is_preferred_over_pat() -> None:
    tx = fg_http.RecordedTransport().add("GET", "/v1/files/", _resp(200, {}))
    _connector(tx, {"FIGMA_TOKEN": "pat", "FIGMA_OAUTH_TOKEN": "oauth"}).call_tool(
        "figma.get_file", {"file_key": "ABC"}
    )
    _, _, headers, _ = tx.calls[0]
    assert headers["authorization"] == "Bearer oauth" and "x-figma-token" not in headers


# --- soft-error envelope -----------------------------------------------------


def test_rate_limit_is_a_soft_error() -> None:
    tx = fg_http.RecordedTransport().add(
        "GET", "/v1/files/", _resp(429, {}, **{"retry-after": "42"})
    )
    out = _connector(tx).call_tool("figma.get_file", {"file_key": "ABC"})
    assert out["ok"] is False and out["error_code"] == "RATE_LIMITED" and out["retry_after"] == 42


def test_upstream_error_is_a_soft_error() -> None:
    tx = fg_http.RecordedTransport().add("GET", "/v1/files/", _resp(404, {"err": "Not found"}))
    out = _connector(tx).call_tool("figma.get_file", {"file_key": "ABC"})
    assert out["ok"] is False and out["error_code"] == "UPSTREAM_ERROR" and out["status"] == 404


def test_unknown_tool_and_missing_arg() -> None:
    conn = _connector(fg_http.RecordedTransport())
    assert conn.call_tool("figma.delete", {})["error_code"] == "UNKNOWN_TOOL"
    assert conn.call_tool("figma.get_file", {})["error_code"] == "BAD_ARGUMENT"


# --- INT-044: variables → tailwind palette -----------------------------------

_VARS = {
    "meta": {
        "variableCollections": {
            "C1": {"id": "C1", "defaultModeId": "M1", "modes": [{"modeId": "M1", "name": "Mode"}]}
        },
        "variables": {
            "V1": {
                "id": "V1",
                "name": "brand/Primary",
                "resolvedType": "COLOR",
                "variableCollectionId": "C1",
                "valuesByMode": {"M1": {"r": 0.1, "g": 0.2, "b": 0.9, "a": 1}},
            },
            "V2": {
                "id": "V2",
                "name": "Surface/Muted",
                "resolvedType": "COLOR",
                "variableCollectionId": "C1",
                "valuesByMode": {"M1": {"r": 1, "g": 1, "b": 1, "a": 0.5}},
            },
            "V3": {  # an ALIAS → V1 (must resolve to the same hex)
                "id": "V3",
                "name": "Brand/Accent",
                "resolvedType": "COLOR",
                "variableCollectionId": "C1",
                "valuesByMode": {"M1": {"type": "VARIABLE_ALIAS", "id": "V1"}},
            },
            "V4": {  # not a color — skipped
                "id": "V4",
                "name": "Spacing/Base",
                "resolvedType": "FLOAT",
                "variableCollectionId": "C1",
                "valuesByMode": {"M1": 8},
            },
        },
    }
}


def test_extract_colors_matches_variables_incl_alias_and_alpha() -> None:
    assert fg_tokens.extract_colors(_VARS) == {
        "brand-primary": "#1a33e6",
        "surface-muted": "rgba(255, 255, 255, 0.5)",
        "brand-accent": "#1a33e6",  # alias resolved to V1
    }


def test_tailwind_config_contains_the_palette() -> None:
    ts = fg_tokens.to_tailwind_config(_VARS)
    assert "export default config" in ts and "colors: {" in ts
    assert '"brand-primary": "#1a33e6",' in ts
    assert '"surface-muted": "rgba(255, 255, 255, 0.5)",' in ts
    assert "Spacing" not in ts  # non-color variable excluded


def test_connector_renders_tailwind_from_a_fetched_file() -> None:
    tx = fg_http.RecordedTransport().add("GET", "/variables/local", _resp(200, _VARS))
    out = _connector(tx).tailwind_config_from_variables({"file_key": "ABC"})
    assert out["ok"] is True and '"brand-primary": "#1a33e6",' in out["tailwind_config"]
