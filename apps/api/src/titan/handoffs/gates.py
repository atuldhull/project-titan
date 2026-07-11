"""Acceptance gates (Vol 6 §2.2, TITAN-WFL-003/004).

A gate is a machine-checkable predicate (optionally plus a consumer-agent review)
that decides whether a handoff artifact may proceed. Every evaluation yields a
:class:`GateResult` carrying the verdict, the reasons, and the evaluator identity
(agent + model + prompt version) the ledger records.

Two gates ship in this phase:
- :func:`validate_schema` — the universal, always-first gate (TITAN-WFL-003):
  schema-validation failure IS gate failure, with a field-level error report.
- :class:`ConfigurableGate` — a deterministic fake gate the tests drive to force
  pass/fail without an LLM, standing in for the concrete G1–G12 predicates that
  land in later phases. The gate registry maps each stage to its gate; unmapped
  stages fall back to the default (auto-pass once the schema validates), so the
  framework is exercised end-to-end before real predicates exist.
"""

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


class Gate(Protocol):
    """A stage's acceptance gate. ``body`` is the already-schema-valid artifact
    payload (validate_schema runs first, so a gate never sees an invalid one)."""

    def evaluate(self, stage: str, body: dict[str, object]) -> GateResult: ...


def validate_schema(
    stage: str, body: dict[str, Any]
) -> tuple[GateResult, dict[str, Any] | None]:
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
    evaluator columns are exercised. Stands in for the real G1–G12 predicates."""

    passes: bool = True
    reasons: list[str] = field(default_factory=list)
    evaluator_agent: str = "code_reviewer"
    evaluator_model: str = "fake-model"
    prompt_version: str = "v0"

    def evaluate(self, stage: str, body: dict[str, object]) -> GateResult:
        return GateResult(
            passed=self.passes,
            reasons=list(self.reasons) if not self.passes else [],
            evaluator_agent=self.evaluator_agent,
            evaluator_model=self.evaluator_model,
            prompt_version=self.prompt_version,
        )


# The default gate: once the schema validates, auto-pass. Concrete per-stage G1–G12
# predicates register here in later phases (Phase 43+).
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


__all__ = [
    "ConfigurableGate",
    "Gate",
    "GateResult",
    "clear_gates",
    "gate_for_stage",
    "register_gate",
    "validate_schema",
]
