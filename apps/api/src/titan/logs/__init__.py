"""Log history over Loki (Vol 4 §6.7, Vol 9 §6.3).

Reads the structured JSON logs promtail already ships to Loki and serves them
to the Logs Viewer. Only ``service`` crosses the module boundary
(TITAN-BCK-004); ``logql``, ``loki`` and ``router`` stay private.

This module owns no tables. Loki is the store, and it is queried over HTTP —
so where every other domain module has a ``repository`` over Postgres, this one
has :mod:`titan.logs.loki` (a port + an httpx adapter) and :mod:`titan.logs.logql`
(the pure query builder that carries the LogQL rules).
"""
