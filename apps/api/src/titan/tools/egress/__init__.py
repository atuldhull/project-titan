"""The sole audited egress path for the sandbox (Phase 23; Vol 8 §5 SEC-042/043).

Two roles in one service (``python -m titan.tools.egress``), both living INSIDE
the ``titan.tools`` module so they append to the hash-chained ``tool_audit``
ledger directly (no import-linter boundary crossed, no new migration):

* the **forward proxy** — the ONLY reachable hop out of the sandbox's deny-all
  internal network. It authenticates each request against a per-task credential,
  allowlists the registry mirror + the task's git remote (+ Admin-approved
  hosts), tunnels/forwards those, and denies everything else with a 403 AND a
  ``SANDBOX_VIOLATION`` audit row attributed to the project/task.
* the **fetch-gateway** — the caching research proxy the trusted web.* tools call
  (server-side fetch, so HTTPS bodies cache with no TLS MITM) plus the caching
  pypi/npm pull-through mirror the sandbox's pip/npm are pointed at.
"""
