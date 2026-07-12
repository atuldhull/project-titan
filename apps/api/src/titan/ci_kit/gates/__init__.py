"""The runnable gates. Every gate is stdlib-only and reports through the same
:class:`GateOutcome`, so the runner, the CI workflow and the kit's own fixture
tests all execute one implementation.

A gate NEVER raises to signal a violation — a violation is a normal result
(``ok=False`` with findings). An exception means the gate itself is broken, and
the runner reports that as an ERROR distinct from a FAIL: a kit that crashes
must not be mistakable for a workspace that passed.
"""

from titan.ci_kit.gates.outcome import Finding, GateOutcome

__all__ = ["Finding", "GateOutcome"]
