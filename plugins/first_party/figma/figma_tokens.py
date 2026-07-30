"""Figma design variables → Tailwind theme (Vol 7 §6.5, TITAN-INT-044). Pure: parses the
``GET /v1/files/{key}/variables/local`` response and emits a ``tailwind.config.ts`` whose
`theme.extend.colors` palette matches the file's COLOR variables — so the Frontend Engineer
agent generates a config grounded in the design source. No I/O ⇒ exhaustively fixture-testable.
"""

import re
from typing import Any

_ALIAS = "VARIABLE_ALIAS"


def _key(name: Any) -> str:
    """A Tailwind-safe color key from a Figma variable name — 'brand/Primary' → 'brand-primary'
    (hierarchy flattened, deterministic)."""
    k = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return k or "color"


def _color(value: Any) -> str | None:
    """A Figma {r,g,b,a} (floats 0..1) → '#rrggbb' (or 'rgba(...)' when translucent)."""
    if not isinstance(value, dict):
        return None
    try:
        r, g, b = (max(0, min(255, round(float(value[c]) * 255))) for c in ("r", "g", "b"))
    except (KeyError, TypeError, ValueError):
        return None
    a = value.get("a", 1)
    try:
        a = float(a)
    except (TypeError, ValueError):
        a = 1.0
    if a < 1:
        return f"rgba({r}, {g}, {b}, {round(a, 3)})"
    return f"#{r:02x}{g:02x}{b:02x}"


def extract_colors(payload: dict[str, Any]) -> dict[str, str]:
    """{tailwind key -> hex/rgba} for every COLOR variable, resolving the default mode and
    one-hop-through variable aliases."""
    meta = (payload or {}).get("meta") or {}
    variables = meta.get("variables") or {}
    collections = meta.get("variableCollections") or {}

    def default_mode(var: dict[str, Any]) -> Any:
        col = collections.get(var.get("variableCollectionId")) or {}
        vbm = var.get("valuesByMode") or {}
        mode = col.get("defaultModeId")
        return mode if mode in vbm else next(iter(vbm), None)

    def resolve(var: Any, depth: int = 0) -> Any:
        if depth > 5 or not isinstance(var, dict):
            return None
        val = (var.get("valuesByMode") or {}).get(default_mode(var))
        if isinstance(val, dict) and val.get("type") == _ALIAS:
            return resolve(variables.get(val.get("id")), depth + 1)
        return val

    out: dict[str, str] = {}
    for var in variables.values():
        if not isinstance(var, dict) or var.get("resolvedType") != "COLOR":
            continue
        hexv = _color(resolve(var))
        if hexv:
            out[_key(var.get("name", ""))] = hexv
    return out


_HEADER = (
    "// Generated from Figma variables by the titan.figma connector (TITAN-INT-044). "
    "Do not edit.\n"
    "import type { Config } from 'tailwindcss'\n\n"
    "const config: Config = {\n"
    "  theme: {\n"
    "    extend: {\n"
    "      colors: {\n"
)
_FOOTER = "      },\n    },\n  },\n}\n\nexport default config\n"


def to_tailwind_config(payload: dict[str, Any]) -> str:
    colors = extract_colors(payload)
    lines = "".join(f'        "{k}": "{colors[k]}",\n' for k in sorted(colors))
    return _HEADER + lines + _FOOTER
