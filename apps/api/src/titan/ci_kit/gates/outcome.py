"""The single result shape every gate reports through.

Per §6.6 TITAN-WFL-033 every finding cites file, line and the violated rule id —
that requirement is on the *reviewer*, but a gate that reports less than the
reviewer must cite would force the reviewer to re-derive it, so the gates meet
the same bar. A finding without a ``wfl_id`` is unrepresentable here.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    """One constitutional violation, cited the way §6.6 requires."""

    wfl_id: str
    path: str
    line: int
    message: str
    fix: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: [{self.wfl_id}] {self.message}\n    fix: {self.fix}"


@dataclass(frozen=True)
class GateOutcome:
    """The result of running one gate over one workspace."""

    gate: str
    wfl_id: str
    ok: bool
    findings: tuple[Finding, ...] = field(default=())
    skipped: str = ""
    """Non-empty when the gate did not apply (e.g. no TypeScript in the
    workspace). A skipped gate is ``ok`` — but it says so, because a gate that
    silently passes when it never ran is indistinguishable from enforcement."""

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def render(self) -> str:
        if self.skipped:
            return f"SKIP {self.gate} ({self.wfl_id}): {self.skipped}"
        if self.ok:
            return f"PASS {self.gate} ({self.wfl_id})"
        lines = [f"FAIL {self.gate} ({self.wfl_id}): {len(self.findings)} violation(s)"]
        lines.extend(f"  {finding.render()}" for finding in self.findings)
        return "\n".join(lines)


def passed(gate: str, wfl_id: str) -> GateOutcome:
    return GateOutcome(gate=gate, wfl_id=wfl_id, ok=True)


def skipped(gate: str, wfl_id: str, why: str) -> GateOutcome:
    return GateOutcome(gate=gate, wfl_id=wfl_id, ok=True, skipped=why)


def failed(gate: str, wfl_id: str, findings: tuple[Finding, ...]) -> GateOutcome:
    return GateOutcome(gate=gate, wfl_id=wfl_id, ok=False, findings=findings)


__all__ = ["Finding", "GateOutcome", "failed", "passed", "skipped"]
