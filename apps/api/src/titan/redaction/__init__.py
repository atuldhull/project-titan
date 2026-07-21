"""The shared redaction pipeline (TITAN-SEC-054/055, Vol 8 §6.4).

One engine, three detectors (live-inventory hashed lookup, provider-key
patterns, high-entropy tokens), replacing matches with
``[REDACTED:<class>:<hash8>]`` — wired into the three mandated sinks (LLM
gateway egress, the structlog→Loki pipeline, sandbox output streams) plus the
Tool Bus envelopes, and the secret-bearing-file exclusion used by the
workspace tools (TITAN-SEC-055).

This package is a strict LEAF: stdlib only, no titan imports (enforced by
import-linter contract "Shared libraries never import domain modules"). The
vault supplies the live inventory and the audit reporter by mutating the
process-global engine at the composition roots — see
:mod:`titan.vault.inventory`.
"""

from titan.redaction.engine import (
    EMPTY_INVENTORY,
    Hit,
    InventoryIndex,
    RedactionEngine,
    hash8,
    redact_structure,
)
from titan.redaction.entropy import is_high_entropy
from titan.redaction.paths import SECRET_FILE_PATTERNS, is_secret_path
from titan.redaction.patterns import DEFAULT_PATTERNS
from titan.redaction.report import RedactionHitRecord, RedactionReporter
from titan.redaction.runtime import default_engine, reset_default_engine

__all__ = [
    "DEFAULT_PATTERNS",
    "EMPTY_INVENTORY",
    "Hit",
    "InventoryIndex",
    "RedactionEngine",
    "RedactionHitRecord",
    "RedactionReporter",
    "SECRET_FILE_PATTERNS",
    "default_engine",
    "hash8",
    "is_high_entropy",
    "is_secret_path",
    "redact_structure",
    "reset_default_engine",
]
