"""Emit the FastAPI OpenAPI document deterministically (TITAN-BCK-027).

Usage: python scripts/export_openapi.py <output-path>

Keys are sorted and the file ends with a newline so regeneration is
byte-stable — the CI drift gate diffs this output against the committed copy.
"""

import json
import sys
from pathlib import Path

from titan.main import create_app


def main() -> None:
    output = Path(sys.argv[1])
    spec = create_app().openapi()
    # newline="\n" keeps the artifact byte-identical across platforms — the
    # drift gate depends on deterministic output.
    output.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    sys.stdout.write(f"wrote {output}\n")


if __name__ == "__main__":
    main()
