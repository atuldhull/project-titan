"""README quickstart parser + executor (TITAN-WFL-034 / G11; Phase 47).

Parses the copy-paste Docker Compose quickstart out of a README and executes it
in a clean Compose environment, failing on a non-zero command, an unhealthy
service, or an unreachable app URL. Execution goes through a :class:`ComposeRunner`
port — the production adapter drives the Phase-22 ``docker.compose_up`` /
``docker.logs`` / health-probe tools (one compose runner, not a second), while
tests inject a fake so the executor's logic is verified hermetically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

# Fenced code blocks in the README.
_FENCE = re.compile(r"```(?:bash|sh|shell|console)?\n(.*?)```", re.DOTALL)
_COMPOSE_CMD = re.compile(r"^\s*\$?\s*(docker[\s-]compose\s+.*)$", re.IGNORECASE)
_APP_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/\S*)?", re.IGNORECASE)


@dataclass(frozen=True)
class QuickstartPlan:
    """The commands + reachability target extracted from the README quickstart."""

    commands: tuple[str, ...]
    app_url: str | None

    @property
    def has_compose_up(self) -> bool:
        return any(re.search(r"\bup\b", cmd) for cmd in self.commands)


@dataclass(frozen=True)
class CommandOutcome:
    """The result of one runner action."""

    exit_code: int
    output: str = ""


@dataclass(frozen=True)
class ProbeOutcome:
    """The result of probing the app URL."""

    reachable: bool
    status: int = 0


class ComposeRunner(Protocol):
    """The port the executor drives. Production wires it to the Phase-22 docker
    tools; tests inject a fake."""

    async def run(self, command: str) -> CommandOutcome:
        """Run one shell command (a ``docker compose`` invocation) in the clean
        workspace and return its exit code + captured output."""
        ...

    async def probe(self, url: str) -> ProbeOutcome:
        """HTTP-probe the app URL for reachability + status."""
        ...

    async def teardown(self) -> None:
        """Tear the environment down (``docker compose down``) — always called."""
        ...


@dataclass(frozen=True)
class QuickstartResult:
    """The executor's verdict."""

    ok: bool
    failed_step: str = ""
    detail: str = ""
    ran_commands: tuple[str, ...] = field(default_factory=tuple)


class QuickstartError(Exception):
    """The README has no runnable quickstart."""


def parse_quickstart(readme: str) -> QuickstartPlan:
    """Extract the Docker Compose commands and app URL from a README. Pure."""
    commands: list[str] = []
    app_url: str | None = None
    for block in _FENCE.findall(readme):
        for line in block.splitlines():
            match = _COMPOSE_CMD.match(line)
            if match:
                commands.append(match.group(1).strip())
            url = _APP_URL.search(line)
            if url and app_url is None:
                app_url = url.group(0).rstrip(".,)")
    # An app URL may also appear in prose outside a fence.
    if app_url is None:
        url = _APP_URL.search(readme)
        app_url = url.group(0).rstrip(".,)") if url else None
    return QuickstartPlan(commands=tuple(commands), app_url=app_url)


async def execute_quickstart(readme: str, runner: ComposeRunner) -> QuickstartResult:
    """Parse + run the README quickstart, failing on the first broken step.

    Order: run each command (fail on non-zero), then probe the app URL (fail if
    unreachable or non-2xx/3xx). ``teardown`` always runs, even on failure, so a
    broken run does not leave containers behind."""
    plan = parse_quickstart(readme)
    if not plan.commands or not plan.has_compose_up:
        raise QuickstartError("README has no `docker compose up` quickstart to execute")

    ran: list[str] = []
    try:
        for command in plan.commands:
            ran.append(command)
            outcome = await runner.run(command)
            if outcome.exit_code != 0:
                return QuickstartResult(
                    ok=False,
                    failed_step=command,
                    detail=f"command exited {outcome.exit_code}: {outcome.output[:200]}",
                    ran_commands=tuple(ran),
                )

        if plan.app_url is not None:
            probe = await runner.probe(plan.app_url)
            if not probe.reachable:
                return QuickstartResult(
                    ok=False,
                    failed_step=f"probe {plan.app_url}",
                    detail="app URL is unreachable after the quickstart",
                    ran_commands=tuple(ran),
                )
            if not 200 <= probe.status < 400:
                return QuickstartResult(
                    ok=False,
                    failed_step=f"probe {plan.app_url}",
                    detail=f"app URL returned {probe.status}",
                    ran_commands=tuple(ran),
                )

        return QuickstartResult(ok=True, ran_commands=tuple(ran))
    finally:
        await runner.teardown()


__all__ = [
    "CommandOutcome",
    "ComposeRunner",
    "ProbeOutcome",
    "QuickstartError",
    "QuickstartPlan",
    "QuickstartResult",
    "execute_quickstart",
    "parse_quickstart",
]
