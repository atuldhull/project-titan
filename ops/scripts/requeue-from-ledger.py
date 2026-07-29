#!/usr/bin/env python
"""Operator entrypoint for run-queue recovery (Vol 9 §8.2 / §9, TITAN-OPS-020).

Rebuilds ``stream:runs.execute`` from the PostgreSQL run ledger after a Redis
queue loss. This is a thin wrapper over titan's tested
``scripts.requeue_from_ledger`` (the logic lives in the app package so it is
import-testable); run it from the API service environment, where ``titan`` and
its ``.env`` are available:

    cd apps/api && uv run --env-file ../../.env \
        python ../../ops/scripts/requeue-from-ledger.py

See RUNBOOK-04 (Vol 9 §12) for the full DR restore order — this is step (4),
"start Redis empty, run ledger re-enqueue".
"""

import os
import sys

# Make the API service's `scripts` package importable when this is invoked from
# the repo root (the real, tested logic lives at apps/api/scripts/).
_API = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "apps", "api"))
if _API not in sys.path:
    sys.path.insert(0, _API)

from scripts.requeue_from_ledger import main  # noqa: E402

if __name__ == "__main__":
    main()
