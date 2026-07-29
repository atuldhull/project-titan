"""Secure-defaults verification (Phase 75, Vol 8 §15, TITAN-SEC-140).

A pure leaf: :class:`SecurityContext` holds the real signals gathered by the
composition root (the CLI), and :func:`evaluate` scores them against the 15
secure-default rows loaded from ``security/compliance-map.yaml``. `titan security
check` prints the scorecard and exits non-zero on any failure.
"""

from titan.security.checker import CHECKS, evaluate, load_secure_default_rows
from titan.security.model import (
    CheckResult,
    CheckStatus,
    Scorecard,
    SecureDefaultRow,
    SecurityContext,
)

__all__ = [
    "CHECKS",
    "CheckResult",
    "CheckStatus",
    "Scorecard",
    "SecureDefaultRow",
    "SecurityContext",
    "evaluate",
    "load_secure_default_rows",
]
