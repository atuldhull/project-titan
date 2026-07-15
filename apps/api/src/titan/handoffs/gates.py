"""Acceptance gates (Vol 6 §2.2, TITAN-WFL-003/004; §2.3 G1–G12).

A gate is a machine-checkable predicate (optionally plus a consumer-agent review)
that decides whether a handoff artifact may proceed. Every evaluation yields a
:class:`GateResult` carrying the verdict, the reasons, and the evaluator identity
(agent + model + prompt version) the ledger records.

The gate framework is deliberately split into two layers:

- The **schema gate** :func:`validate_schema` — the universal, always-first gate
  (TITAN-WFL-003): schema-validation failure IS gate failure, with a field-level
  error report. It runs in the executor before the acceptance gate.
- The **acceptance gate**, a :class:`Gate` evaluated against a :class:`GateContext`.
  The context carries everything a gate needs beyond its own artifact — prior-stage
  artifacts (for cross-artifact traceability checks), the active constitution (for
  G5), sibling active projects (for G1 duplicate detection), and tunables — so the
  gate predicates stay **pure and synchronous** (no DB, no I/O), and therefore
  deterministically fixture-testable. The executor assembles the context (the only
  place that touches the database) and calls the pure gate.

:class:`ConfigurableGate` is a deterministic fake gate the tests drive to force
pass/fail without an evaluator; the concrete G1–G5 predicates live in
``standard_gates``. The registry maps each stage to its gate; unmapped stages fall
back to the default (auto-pass once the schema validates).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from titan.handoffs.schemas import schema_for_stage


@dataclass(frozen=True)
class GateResult:
    """The outcome of one gate evaluation."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    #: Evaluator identity persisted in the ledger (TITAN-WFL-004).
    evaluator_agent: str = "orchestrator"
    evaluator_model: str = "none"
    prompt_version: str = "n/a"
    #: The constitution version this gate judged against (G5 only — §2.3 H5 DoD).
    constitution_version: int | None = None


@dataclass(frozen=True)
class GateContext:
    """Everything a pure gate predicate needs to evaluate one handoff. The executor
    assembles it (the sole DB touchpoint); gates read it and stay side-effect-free.

    ``body`` is the already-schema-valid, normalized artifact payload of ``stage``.
    ``prior_artifacts`` maps each earlier producing stage to its latest normalized
    artifact body (for cross-artifact checks — e.g. G3 story→feature coverage,
    G4 story→task mapping, G5 task→component coverage)."""

    stage: str
    body: Mapping[str, Any]
    prior_artifacts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Other non-archived projects' request/brief text (normalized) — G1 dedup.
    active_project_texts: tuple[str, ...] = ()
    #: The active constitution G5 judges against (retrieved into context, not
    #: hardcoded). ``constitution_version`` is stamped onto the G5 ledger row.
    constitution_version: int | None = None
    constitution_body: str = ""
    #: Per-sprint capacity in ideal agent-days (G4 capacity check).
    sprint_capacity_days: float = 5.0
    project_id: uuid.UUID | None = None
    #: Execution-side results the executor computed for gates whose artifact does
    #: not itself record them: G6 reads ``checks["ci"]`` (lint/typecheck/unit/
    #: integration/coverage) + ``checks["migration"]`` (fresh + snapshot apply), and
    #: G11 reads ``checks["doc"]`` (the §6.7 coverage report). The gate stays pure —
    #: it only READS the pre-computed results; the executor runs the real checks.
    checks: Mapping[str, Any] = field(default_factory=dict)


class Gate(Protocol):
    """A stage's acceptance gate. Pure: it reads only its :class:`GateContext` and
    returns a :class:`GateResult` — no I/O, so it is deterministically testable."""

    def evaluate(self, ctx: GateContext) -> GateResult: ...


def validate_schema(stage: str, body: dict[str, Any]) -> tuple[GateResult, dict[str, Any] | None]:
    """The universal schema gate (TITAN-WFL-003). Returns ``(result, normalized)``:
    on success ``normalized`` is the round-tripped artifact payload (canonical,
    defaults filled); on failure ``result.passed`` is False with a field-level
    error report and ``normalized`` is None — schema failure counts as gate
    failure. An unknown stage (no schema) fails closed."""
    schema = schema_for_stage(stage)
    if schema is None:
        return GateResult(
            passed=False, reasons=[f"no artifact schema is defined for stage {stage}"]
        ), None
    try:
        model = schema.model_validate(body)
    except ValidationError as exc:
        return GateResult(passed=False, reasons=_field_errors(exc)), None
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return GateResult(passed=True), dumped


def _field_errors(exc: ValidationError) -> list[str]:
    """A field-level error report (TITAN-WFL-003 acceptance): 'field.path: message'
    per invalid field, deterministically ordered."""
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        out.append(f"{loc}: {err['msg']}")
    return sorted(out)


@dataclass
class ConfigurableGate:
    """A deterministic fake gate (tests + the framework default). Passes unless
    configured to fail; carries a fake evaluator identity so the ledger's
    evaluator columns are exercised. Stands in for the real G1–G12 predicates when
    a stage has no registered gate."""

    passes: bool = True
    reasons: list[str] = field(default_factory=list)
    evaluator_agent: str = "code_reviewer"
    evaluator_model: str = "fake-model"
    prompt_version: str = "v0"

    def evaluate(self, ctx: GateContext) -> GateResult:
        return GateResult(
            passed=self.passes,
            reasons=list(self.reasons) if not self.passes else [],
            evaluator_agent=self.evaluator_agent,
            evaluator_model=self.evaluator_model,
            prompt_version=self.prompt_version,
        )


# The default gate: once the schema validates, auto-pass. Stages with a concrete
# G1–G12 predicate register it (``standard_gates.register_standard_gates``);
# unregistered stages (G6–G12, not yet implemented) fall back to this default.
_DEFAULT_GATE: Gate = ConfigurableGate(passes=True)
_GATES: dict[str, Gate] = {}


def gate_for_stage(stage: str) -> Gate:
    """The acceptance gate registered for ``stage`` (or the auto-pass default)."""
    return _GATES.get(stage, _DEFAULT_GATE)


def register_gate(stage: str, gate: Gate) -> None:
    """Register (or override, for tests) a stage's acceptance gate."""
    _GATES[stage] = gate


def clear_gates() -> None:
    """Reset the registry to defaults — test isolation."""
    _GATES.clear()


def snapshot_gates() -> dict[str, Gate]:
    """A copy of the current registry — pair with :func:`restore_gates` so a caller
    that must (re)register gates can put the process-global registry back exactly as
    it found it, instead of clearing it to the auto-pass default (which would make a
    co-running test's gates silently pass)."""
    return dict(_GATES)


def restore_gates(snapshot: dict[str, Gate]) -> None:
    """Restore the registry to a :func:`snapshot_gates` result."""
    _GATES.clear()
    _GATES.update(snapshot)


__all__ = [
    "ConfigurableGate",
    "Gate",
    "GateContext",
    "GateResult",
    "clear_gates",
    "gate_for_stage",
    "register_gate",
    "restore_gates",
    "snapshot_gates",
    "validate_schema",
]
