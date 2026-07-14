"""Emit the plugin-manifest JSON Schema deterministically (Vol 7 §3.2, TITAN-INT-010).

Usage: python scripts/export_plugin_schema.py <output-path>

The manifest schema is PUBLISHED so third parties can validate `titan-plugin.yaml`
before install. Keys are sorted and the file ends with a newline so regeneration is
byte-stable — the CI drift gate diffs this output against the committed copy, exactly
like scripts/export_openapi.py does for the OpenAPI document.
"""

import json
import sys
from pathlib import Path

from titan.plugins.manifest import json_schema


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python scripts/export_plugin_schema.py <output-path>\n")
        raise SystemExit(2)
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps the artifact byte-identical across platforms — the drift
    # gate depends on deterministic output.
    output.write_text(
        json.dumps(json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
