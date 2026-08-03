"""Evolution framework (Vol 10 — Future & Autonomous Evolution).

The human-gated self-improvement substrate: versioned ``/evolution/**`` artifacts
(prompts, routing policy, scoring weights, benchmark defs), a SHA-pinned manifest
loader, and the Evolution-Proposal (EP) approval gate (ADR-EVO-01).

Submodules are imported explicitly by their consumers so this package stays a
low-level library: :mod:`titan.evolution.manifest` and :mod:`titan.evolution.resolver`
are pure (stdlib + Jinja only) and are imported by the prompt composer; the DB
models / repository / service / router are composition-root concerns imported by
``main.py``. Nothing here imports a domain module (TITAN-BCK-001).
"""
