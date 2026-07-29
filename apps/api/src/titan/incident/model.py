"""Data model for incident lockdown + the AI-specific playbooks (TITAN-SEC-120)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionResult:
    """One containment action taken during a lockdown or targeted response."""

    action: str
    target: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class LockdownReport:
    reason: str
    actor: str
    actions: list[ActionResult] = field(default_factory=list)

    @property
    def contained(self) -> bool:
        """The drill is a success iff EVERY action landed — a partial lockdown is
        not containment."""
        return bool(self.actions) and all(a.ok for a in self.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "actor": self.actor,
            "contained": self.contained,
            "actions": [
                {"action": a.action, "target": a.target, "ok": a.ok, "detail": a.detail}
                for a in self.actions
            ],
        }


@dataclass(frozen=True)
class PlaybookStep:
    order: int
    description: str
    automated: bool  # True = this step is executed by a lockdown/targeted command


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    trigger: str
    steps: list[PlaybookStep]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "trigger": self.trigger,
            "steps": [
                {"order": s.order, "description": s.description, "automated": s.automated}
                for s in self.steps
            ],
        }
