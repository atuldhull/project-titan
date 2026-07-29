"""Incident lockdown orchestration + the four AI-specific playbooks (TITAN-SEC-120,
Vol 8 §13). A pure leaf: every side effect (flip a kill switch, revoke tokens, kill a
sandbox, rotate a secret, write the audit row) is an INJECTED callback, so the module
stays domain-independent and the CLI composition root wires the real implementations.

``full_lockdown`` is the §13.2 one-command containment: pause all agent execution
(the DoD — halts the dispatch loop ≤ 60 s via the Phase-72 ``agents.pause_all`` kill
switch), set intake read-only (freezing new work AND, transitively, the pipeline that
drives deploys), and revoke agent + privileged tokens. Every toggle is audited.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from titan.incident.model import ActionResult, LockdownReport, Playbook, PlaybookStep

# Kill-switch keys the lockdown flips. Stable cross-module protocol constants (the
# module cannot import titan.flags without breaking the independence contract); they
# mirror titan.flags.service.{PAUSE_ALL, INTAKE_READ_ONLY}.
PAUSE_ALL_KEY = "agents.pause_all"
INTAKE_READ_ONLY_KEY = "intake.read_only"

FlagFlip = Callable[[str, bool, str | None], Awaitable[None]]
Nullary = Callable[[], Awaitable[None]]
AuditHook = Callable[[str, str, dict[str, object]], Awaitable[None]]


@dataclass(frozen=True)
class LockdownDeps:
    """The real side effects, injected by the composition root."""

    flip_flag: FlagFlip
    revoke_agents: Nullary
    revoke_privileged: Nullary
    revoke_sessions: Nullary  # durably revoke ALL refresh tokens + API keys (review #1)
    revoke_family: Callable[[str], Awaitable[None]]
    kill_sandboxes: Callable[[str], Awaitable[int]]  # returns the count destroyed
    disable_plugin: Callable[[str], Awaitable[None]]
    rotate_secret: Callable[[str, str], Awaitable[None]]
    audit: AuditHook  # (resource, outcome, detail) for AuditAction.INCIDENT_TOGGLE


class IncidentService:
    def __init__(self, deps: LockdownDeps) -> None:
        self._d = deps

    async def _do(self, action: str, target: str, run: Nullary, detail: str) -> ActionResult:
        """Run one containment action, ALWAYS auditing the outcome (a failed toggle
        must still leave a record). Never raises — a partial failure is reported, not
        thrown, so lockdown presses on with the remaining actions."""
        try:
            await run()
            result = ActionResult(action, target, True, detail)
        except Exception as exc:  # noqa: BLE001 — containment must be best-effort
            result = ActionResult(action, target, False, f"{detail} FAILED: {exc}")
        # The audit write is ALSO best-effort: a degraded DB during an incident (the
        # exact time lockdown runs) must NOT abort the fan-out before the remaining
        # actions — token revocation especially — get to run (SEC-120 review #3).
        outcome = "success" if result.ok else "failure"
        try:
            await self._d.audit(f"{action}:{target}", outcome, {"detail": result.detail})
        except Exception:  # noqa: BLE001
            return ActionResult(action, target, result.ok, f"{result.detail} (audit write failed)")
        return result

    async def full_lockdown(self, *, reason: str, actor: str) -> LockdownReport:
        actions = [
            await self._do(
                "flag.enable",
                PAUSE_ALL_KEY,
                lambda: self._d.flip_flag(PAUSE_ALL_KEY, True, None),
                "paused ALL agent execution (dispatch loop halts <=60s)",
            ),
            await self._do(
                "flag.enable",
                INTAKE_READ_ONLY_KEY,
                lambda: self._d.flip_flag(INTAKE_READ_ONLY_KEY, True, None),
                "set intake read-only (freezes new work + pipeline-driven deploys)",
            ),
            await self._do(
                "token.revoke_agents",
                "*",
                self._d.revoke_agents,
                "revoked all agent service tokens",
            ),
            await self._do(
                "token.revoke_privileged",
                "*",
                self._d.revoke_privileged,
                "revoked active privileged access tokens (epoch)",
            ),
            await self._do(
                "session.revoke_all",
                "*",
                self._d.revoke_sessions,
                "revoked ALL refresh tokens + API keys (forces re-auth; closes the "
                "long-lived-credential bypass)",
            ),
        ]
        return LockdownReport(reason=reason, actor=actor, actions=actions)

    async def revoke_token_family(self, family_id: str) -> ActionResult:
        return await self._do(
            "token.revoke_family",
            family_id,
            lambda: self._d.revoke_family(family_id),
            f"revoked refresh family {family_id}",
        )

    async def kill_sandbox(self, task_id: str) -> ActionResult:
        killed = 0

        async def run() -> None:
            nonlocal killed
            killed = await self._d.kill_sandboxes(task_id)

        result = await self._do("sandbox.kill", task_id, run, f"destroyed sandboxes for {task_id}")
        if result.ok:
            return ActionResult(result.action, task_id, True, f"destroyed {killed} sandbox(es)")
        return result

    async def disable_plugin(self, plugin_id: str) -> ActionResult:
        return await self._do(
            "plugin.disable",
            plugin_id,
            lambda: self._d.disable_plugin(plugin_id),
            f"disabled plugin {plugin_id}",
        )

    async def rotate_secret(self, name: str, value: str) -> ActionResult:
        return await self._do(
            "secret.rotate",
            name,
            lambda: self._d.rotate_secret(name, value),
            f"rotated secret {name}",
        )


# --- §13.3 AI-specific playbooks (executable documents) ----------------------

PLAYBOOKS: dict[str, Playbook] = {
    "P1": Playbook(
        id="P1",
        title="Prompt-injection incident",
        trigger="An agent acted on injected/untrusted content (README, issue, web page).",
        steps=[
            PlaybookStep(1, "Quarantine the offending content (mark the source untrusted).", False),
            PlaybookStep(
                2, "Replay the task trace by trace_id to map influenced tool calls.", False
            ),
            PlaybookStep(3, "Revert downstream artifacts produced under influence.", False),
            PlaybookStep(4, "Add the exploit payload to the red-team corpus (SEC-131).", False),
        ],
    ),
    "P2": Playbook(
        id="P2",
        title="Malicious generated code merged",
        trigger="Code that fails a gate reached a branch/deployment.",
        steps=[
            PlaybookStep(1, "Locate affected branches/deployments via artifact digests.", False),
            PlaybookStep(2, "Auto-open revert PRs for every carrier.", False),
            PlaybookStep(3, "Rescan history; review which gate missed it.", False),
            PlaybookStep(4, "kill-sandbox any sandbox that ran the code.", True),
        ],
    ),
    "P3": Playbook(
        id="P3",
        title="Secret in LLM context",
        trigger="A secret was sent to a provider (provider logs are outside our control).",
        steps=[
            PlaybookStep(1, "rotate-secret immediately — assume compromised.", True),
            PlaybookStep(2, "Audit which providers received it.", False),
            PlaybookStep(3, "Strengthen the redaction rule for that secret class.", False),
            PlaybookStep(4, "Notify org Admins per the disclosure policy.", False),
        ],
    ),
    "P4": Playbook(
        id="P4",
        title="Memory poisoning",
        trigger="A tainted actor/window injected records into memory.",
        steps=[
            PlaybookStep(1, "Freeze tier promotions (lockdown pauses all agents).", True),
            PlaybookStep(
                2, "Query memory by provenance for records from the tainted actor/window.", False
            ),
            PlaybookStep(3, "Purge the tainted records.", False),
            PlaybookStep(4, "Rebuild derived embeddings from the clean set.", False),
        ],
    ),
}
