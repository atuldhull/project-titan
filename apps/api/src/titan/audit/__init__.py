"""Immutable, hash-chained platform audit ledger (Vol 8 §10, SEC-090..094).

Public surface is ``titan.audit.service`` (the cross-boundary import point per
TITAN-BCK-001) — the one shared audit client every subsystem records through.
``titan.audit.router`` is attached by main.py.
"""
