"""Documentation gates (Vol 6 §6.7, TITAN-WFL-034; Phase 47).

The machine-checkable half of the documentation deliverable: the doc-coverage
checker (every public endpoint documented with prose, every ADR indexed, a README
Docker Compose quickstart present) and the quickstart executor (the quickstart
runs cleanly in a real Compose environment). The coverage report becomes gate
G11's input in a later phase.
"""

from titan.docs.doc_coverage import DocCoverageReport, check_doc_coverage
from titan.docs.quickstart import (
    QuickstartPlan,
    QuickstartResult,
    execute_quickstart,
    parse_quickstart,
)

__all__ = [
    "DocCoverageReport",
    "QuickstartPlan",
    "QuickstartResult",
    "check_doc_coverage",
    "execute_quickstart",
    "parse_quickstart",
]
