"""terminal.* sandbox tests (Phase 21; Vol 7 §5, TITAN-INT-031).

Marked ``sandbox``: needs Docker + the titan-sandbox image. Proves argv-only
literal execution, 1 MiB output cap + truncation, timeout kill, background
run/kill, and — the DoD — incremental tool.output streaming over pub/sub seen
by a subscriber.
"""

import asyncio
import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest

from titan.tools.builtin import terminal

from ._sandbox_tools import ToolHarness, docker_ready
from .conftest import run_async

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not docker_ready():
        pytest.skip("no Docker / titan-sandbox:local image for the terminal suite")


@pytest.fixture
def harness() -> Iterator[ToolHarness]:
    h = ToolHarness()
    try:
        yield h
    finally:
        h.close()


def _run(harness: ToolHarness, args: dict[str, Any]) -> Any:
    return run_async(lambda: terminal._run(args, harness.ctx())).result


def test_hostile_argv_executes_literally(harness: ToolHarness) -> None:
    (harness.workspace.root / "safe.txt").write_text("intact")
    # A shell-metachar "filename" is a literal argv element, never interpreted.
    result = _run(harness, {"argv": ["echo", "; rm -rf /workspace/safe.txt"]})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "; rm -rf /workspace/safe.txt"
    assert (harness.workspace.root / "safe.txt").read_text() == "intact"


def test_output_is_capped_and_marked(harness: ToolHarness) -> None:
    # A yes-spam floods stdout; it is capped near 1 MiB, flagged truncated, and
    # killed at the timeout (never runs unbounded).
    result = _run(harness, {"argv": ["yes", "spamspamspam"], "timeout_s": 5})
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= 1024 * 1024 + 4096
    assert result["timed_out"] is True


def test_timeout_kills_a_runaway(harness: ToolHarness) -> None:
    result = _run(harness, {"argv": ["sleep", "30"], "timeout_s": 2})
    assert result["timed_out"] is True and result["exit_code"] != 0


def test_env_and_cwd_are_honored(harness: ToolHarness) -> None:
    (harness.workspace.root / "sub").mkdir()
    result = _run(
        harness,
        {
            "argv": ["sh", "-c", "echo $GREETING in $(pwd)"],
            "env": {"GREETING": "hello"},
            "cwd": "sub",
        },
    )
    assert "hello in /workspace/sub" in result["stdout"]


def test_cwd_escape_is_jailed(harness: ToolHarness) -> None:
    from titan.tools.errors import ToolError

    with pytest.raises(ToolError) as exc:
        _run(harness, {"argv": ["pwd"], "cwd": "../../etc"})
    assert exc.value.code == "SANDBOX_VIOLATION"


def test_background_run_and_kill(harness: ToolHarness) -> None:
    started = run_async(
        lambda: terminal._run_background({"argv": ["sleep", "60"]}, harness.ctx())
    ).result
    handle = str(started["handle"])
    assert handle.isdigit() and int(handle) > 0
    killed = run_async(
        lambda: terminal._kill({"handle": handle, "signal": "KILL"}, harness.ctx())
    ).result
    assert killed["killed"] is True


# --- streaming pub/sub (DoD) --------------------------------------------------


def test_terminal_output_streams_over_pubsub(harness: ToolHarness) -> None:
    from titan.tools.streaming import channel_for

    channel = channel_for(harness.project_id)

    async def scenario() -> list[Any]:
        sub = harness.redis.pubsub()
        await sub.subscribe(channel)
        events: list[Any] = []

        async def reader() -> None:
            # A subscriber must read CONCURRENTLY with the publisher (a live
            # dashboard tail) — pub/sub is fire-and-forget, not buffered.
            async for message in sub.listen():
                if message["type"] == "message":
                    events.append(json.loads(message["data"]))

        reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0)  # let the subscriber start listening
        # Emit lines with delays so the docker daemon flushes SEPARATE frames —
        # proving incremental (not one-blob) delivery.
        await terminal._run(
            {"argv": ["sh", "-c", "for i in 1 2 3; do echo line$i; sleep 0.15; done"]},
            harness.ctx(),
        )
        await asyncio.sleep(0.3)  # let the tail drain
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        await sub.aclose()  # type: ignore[no-untyped-call]
        return events

    events = run_async(scenario)
    assert events, "no tool.output events were published"
    assert all(e["type"] == "tool.output" for e in events)
    assert all(e["tool_call_id"] == "tc_test" for e in events)
    # The incremental chunks reconstruct the command's output.
    combined = "".join(e["chunk"] for e in events if e["stream"] == "stdout")
    assert "line1" in combined and "line3" in combined
    # INCREMENTAL, not one blob: the delayed lines arrive as multiple frames,
    # with strictly increasing sequence numbers.
    stdout_events = [e for e in events if e["stream"] == "stdout"]
    assert len(stdout_events) >= 2, "output was not delivered incrementally"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.schema
def test_terminal_run_through_the_bus_is_audited(
    migrated_engine: Any, harness: ToolHarness
) -> None:
    # The full wiring: build_bus(sandbox_manager, redis) -> ctx.sandbox -> the
    # terminal executor, with the call landing in the immutable audit ledger.
    from sqlalchemy import text

    from titan.tools import repository as audit_repo
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call
    from titan.tools.service import build_registry

    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("TRUNCATE tool_audit"))

    run_async(clean)

    bus = ToolBus(
        build_registry(),
        migrated_engine,
        approval_gate=deny_all_approvals,
        workspace_provider=_ManagerWorkspaces(harness),
        sandbox_manager=harness.manager,
        redis=harness.redis,
    )
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=harness.project_id,
        granted_scopes=frozenset({"terminal:run", "workspace:read", "workspace:write"}),
        task_id=harness.task_id,
    )
    call = new_call(
        "terminal.run",
        {"argv": ["echo", "wired"]},
        project_id=harness.project_id,
        agent_id=caller.agent_id,
        task_id=harness.task_id,
    )
    result = run_async(lambda: bus.invoke(call, caller))
    assert result.status == "ok" and result.result["stdout"].strip() == "wired"

    async def rows() -> list[Any]:
        async with migrated_engine.connect() as conn:
            return await audit_repo.rows_for_call(conn, call.tool_call_id)

    audited = run_async(rows)
    assert [r.status for r in audited] == ["started", "ok"]


class _ManagerWorkspaces:
    """A WorkspaceProvider that hands the bus the harness's single workspace."""

    def __init__(self, harness: ToolHarness) -> None:
        self._workspace = harness.workspace

    def for_call(self, project_id: Any, task_id: Any) -> Any:
        return self._workspace
