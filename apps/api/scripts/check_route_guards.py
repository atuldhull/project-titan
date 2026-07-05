"""CI route-guard check: fail if any API route lacks an RBAC guard.

Builds the real application and introspects every registered route
(TITAN-BCK-021 / TITAN-SEC-030). Exit 1 (with the offending routes) if any
non-public route omits a Requires/RequiresAuth dependency.

Usage: uv run python scripts/check_route_guards.py
"""

import sys

from titan.gateway.route_guard import format_report, unprotected_routes
from titan.main import create_app
from titan.settings import Settings


def main() -> None:
    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))
    offenders = unprotected_routes(app)
    if offenders:
        sys.stderr.write(format_report(offenders) + "\n")
        raise SystemExit(1)
    sys.stdout.write("route-guard check OK: every API route is protected or public\n")


if __name__ == "__main__":
    main()
