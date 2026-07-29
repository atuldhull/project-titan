"""Phase 75 (Vol 8 §13): incident lockdown + the AI playbooks (TITAN-SEC-120).

DoD: `titan incident lockdown` pauses ALL agent execution and revokes agent +
privileged tokens, auditing every toggle; a partial failure is reported (not thrown)
and still audited. The token-epoch sweeps are re-broken against fakeredis: an agent /
privileged token minted before the cutoff is revoked; others are not. Pure — no DB.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis

from titan.identity import tokens
from titan.incident import IncidentService, LockdownDeps
from titan.incident.service import INTAKE_READ_ONLY_KEY, PAUSE_ALL_KEY, PLAYBOOKS

pytestmark: list[Any] = []


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Recorder:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.flags: dict[str, bool] = {}
        self.calls: list[str] = []
        self.audits: list[tuple[str, str]] = []
        self._fail = fail or set()

    def deps(self) -> LockdownDeps:
        async def flip(key: str, enabled: bool, value: str | None) -> None:
            self.calls.append(f"flip:{key}")
            self.flags[key] = enabled

        async def revoke_agents() -> None:
            self.calls.append("revoke_agents")
            if "revoke_agents" in self._fail:
                raise RuntimeError("redis down")

        async def revoke_privileged() -> None:
            self.calls.append("revoke_privileged")

        async def revoke_sessions() -> None:
            self.calls.append("revoke_sessions")

        async def revoke_family(fam: str) -> None:
            self.calls.append(f"revoke_family:{fam}")

        async def kill(task: str) -> int:
            self.calls.append(f"kill:{task}")
            return 3

        async def disable_plugin(pid: str) -> None:
            self.calls.append(f"disable:{pid}")

        async def rotate_secret(name: str, value: str) -> None:
            self.calls.append(f"rotate:{name}")

        async def audit(resource: str, outcome: str, detail: dict[str, object]) -> None:
            self.audits.append((resource, outcome))
            if "audit" in self._fail:
                raise RuntimeError("audit DB down")

        return LockdownDeps(
            flip_flag=flip,
            revoke_agents=revoke_agents,
            revoke_privileged=revoke_privileged,
            revoke_sessions=revoke_sessions,
            revoke_family=revoke_family,
            kill_sandboxes=kill,
            disable_plugin=disable_plugin,
            rotate_secret=rotate_secret,
            audit=audit,
        )


def test_full_lockdown_flips_pauses_revokes_and_audits_every_action() -> None:
    rec = _Recorder()
    report = _run(IncidentService(rec.deps()).full_lockdown(reason="drill", actor="op"))
    assert report.contained
    assert rec.flags[PAUSE_ALL_KEY] is True  # the DoD: all agent execution paused
    assert rec.flags[INTAKE_READ_ONLY_KEY] is True
    assert "revoke_agents" in rec.calls and "revoke_privileged" in rec.calls
    # review #1: the long-lived-credential sweep (refresh tokens + API keys) also runs.
    assert "revoke_sessions" in rec.calls
    # Every one of the 5 actions produced an audit row, all success.
    assert len(rec.audits) == 5
    assert all(outcome == "success" for _, outcome in rec.audits)


def test_partial_failure_is_reported_not_thrown_and_still_audited() -> None:
    rec = _Recorder(fail={"revoke_agents"})
    report = _run(IncidentService(rec.deps()).full_lockdown(reason="drill", actor="op"))
    assert not report.contained  # a partial lockdown is NOT containment
    # But lockdown pressed on: pause_all still flipped, and the failure was audited.
    assert rec.flags[PAUSE_ALL_KEY] is True
    failed = [a for a in report.actions if not a.ok]
    assert len(failed) == 1 and failed[0].action == "token.revoke_agents"
    assert ("token.revoke_agents:*", "failure") in rec.audits


def test_audit_failure_does_not_abort_the_lockdown_fanout() -> None:
    # review #3: a degraded audit DB during an incident must NOT stop containment.
    rec = _Recorder(fail={"audit"})
    report = _run(IncidentService(rec.deps()).full_lockdown(reason="drill", actor="op"))
    # Despite EVERY audit write failing, all 5 actions still ran (pause + revokes).
    assert rec.flags[PAUSE_ALL_KEY] is True
    assert "revoke_agents" in rec.calls and "revoke_sessions" in rec.calls
    assert len(report.actions) == 5


def test_targeted_actions_call_the_dep_and_audit() -> None:
    rec = _Recorder()
    svc = IncidentService(rec.deps())
    assert _run(svc.revoke_token_family("fam-1")).ok
    assert _run(svc.kill_sandbox("task-9")).detail == "destroyed 3 sandbox(es)"
    assert _run(svc.disable_plugin("plg")).ok
    assert _run(svc.rotate_secret("db-pw", "new")).ok
    assert "revoke_family:fam-1" in rec.calls and "kill:task-9" in rec.calls
    assert len(rec.audits) == 4


def test_playbooks_cover_p1_through_p4() -> None:
    assert set(PLAYBOOKS) == {"P1", "P2", "P3", "P4"}
    for pb in PLAYBOOKS.values():
        assert len(pb.steps) == 4 and pb.trigger


# --- the token-epoch fleet-revocation sweeps (fakeredis) ---------------------


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _claims(**over: Any) -> dict[str, Any]:
    base = {"jti": "j1", "iat": 1000, "roles": ["operator"]}
    base.update(over)
    return base


def test_revoke_agents_before_revokes_only_old_agent_tokens() -> None:
    async def scenario() -> None:
        redis = _redis()
        await tokens.revoke_agents_before(redis, cutoff_epoch=1500)
        # An agent token minted before the cutoff → revoked.
        assert await tokens.is_revoked(redis, _claims(agent_id="a1", iat=1000))
        # An agent token minted AFTER the cutoff → not revoked.
        assert not await tokens.is_revoked(redis, _claims(agent_id="a1", iat=2000))
        # A NON-agent (human) token with the same old iat → not swept.
        assert not await tokens.is_revoked(redis, _claims(iat=1000))
        await redis.aclose()

    _run(scenario())


def test_revoke_privileged_before_sweeps_only_privileged_sessions() -> None:
    async def scenario() -> None:
        redis = _redis()
        await tokens.revoke_privileged_before(redis, cutoff_epoch=1500)
        assert await tokens.is_revoked(redis, _claims(roles=["admin"], iat=1000))
        assert await tokens.is_revoked(redis, _claims(roles=["owner"], iat=1000))
        # A viewer/operator session is not forced to re-auth.
        assert not await tokens.is_revoked(redis, _claims(roles=["viewer"], iat=1000))
        # A privileged session minted after the cutoff survives.
        assert not await tokens.is_revoked(redis, _claims(roles=["admin"], iat=2000))
        await redis.aclose()

    _run(scenario())
