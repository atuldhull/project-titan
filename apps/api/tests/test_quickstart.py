"""The README quickstart parser + executor (Phase 47, TITAN-WFL-034). Hermetic:
a fake ComposeRunner stands in for the Phase-22 docker tools so the executor's
success/failure logic is verified without Docker."""

import pytest

from tests.conftest import run_async
from titan.docs.quickstart import (
    CommandOutcome,
    ProbeOutcome,
    QuickstartError,
    execute_quickstart,
    parse_quickstart,
)

_README = """
# Service

## Quickstart

```bash
docker compose up -d --build
docker compose ps
```

Visit http://localhost:8000/healthz once it is up.
"""


class FakeRunner:
    """A scripted ComposeRunner: per-command exit codes + a probe result."""

    def __init__(
        self, *, exit_codes: dict[str, int] | None = None, probe: ProbeOutcome | None = None
    ) -> None:
        self._exit_codes = exit_codes or {}
        self._probe = probe or ProbeOutcome(reachable=True, status=200)
        self.ran: list[str] = []
        self.torn_down = False

    async def run(self, command: str) -> CommandOutcome:
        self.ran.append(command)
        return CommandOutcome(exit_code=self._exit_codes.get(command, 0), output="ok")

    async def probe(self, url: str) -> ProbeOutcome:
        return self._probe

    async def teardown(self) -> None:
        self.torn_down = True


# --- parsing -----------------------------------------------------------------


def test_parse_extracts_commands_and_app_url() -> None:
    plan = parse_quickstart(_README)
    assert plan.commands == ("docker compose up -d --build", "docker compose ps")
    assert plan.app_url == "http://localhost:8000/healthz"
    assert plan.has_compose_up


def test_parse_readme_without_quickstart_yields_no_commands() -> None:
    plan = parse_quickstart("# Service\n\nNo compose here.\n")
    assert plan.commands == ()
    assert not plan.has_compose_up


# --- execution ---------------------------------------------------------------


def test_working_quickstart_passes_and_tears_down() -> None:
    runner = FakeRunner()
    result = run_async(lambda: execute_quickstart(_README, runner))
    assert result.ok, result.detail
    assert runner.ran == ["docker compose up -d --build", "docker compose ps"]
    assert runner.torn_down


def test_nonzero_command_fails_the_quickstart() -> None:
    runner = FakeRunner(exit_codes={"docker compose up -d --build": 1})
    result = run_async(lambda: execute_quickstart(_README, runner))
    assert not result.ok
    assert result.failed_step == "docker compose up -d --build"
    assert runner.torn_down  # cleanup still runs on failure


def test_unreachable_app_url_fails_the_quickstart() -> None:
    runner = FakeRunner(probe=ProbeOutcome(reachable=False))
    result = run_async(lambda: execute_quickstart(_README, runner))
    assert not result.ok
    assert "unreachable" in result.detail


def test_unhealthy_status_fails_the_quickstart() -> None:
    runner = FakeRunner(probe=ProbeOutcome(reachable=True, status=503))
    result = run_async(lambda: execute_quickstart(_README, runner))
    assert not result.ok
    assert "503" in result.detail


def test_readme_without_a_quickstart_raises() -> None:
    runner = FakeRunner()
    with pytest.raises(QuickstartError):
        run_async(lambda: execute_quickstart("# Service\n\nNothing to run.\n", runner))
