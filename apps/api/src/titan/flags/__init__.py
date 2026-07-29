"""Self-hosted feature flags + ops kill switches (Vol 9 §5.3, TITAN-OPS-012).

Public surface is ``titan.flags.service`` (the cross-boundary import point per
TITAN-BCK-001); ``titan.flags.router`` is attached by main.py.
"""
