"""The ``titan benchmark`` CLI (Phase 55). ``list`` is pure; ``run`` executes the
brief end-to-end over the real schema with a metered mock gateway (schema-marked),
proving the CLI emits a complete report and exits 0 when the MVP thresholds hold."""

import json
from pathlib import Path
from typing import Any

import pytest

from titan import cli

pytestmark = pytest.mark.schema


def test_cli_list_enumerates_the_fixed_suite(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["benchmark", "list"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["briefs"]) == 10
    executable = [b for b in payload["briefs"] if b["executable"]]
    assert len(executable) == 1 and executable[0]["id"] == "todo-tiny"


def test_cli_run_emits_report_and_exits_zero(
    migrated_engine: Any,
    schema_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fakeredis.aioredis
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.benchmark_helpers import metered_gateway
    from titan import clients
    from titan.handoffs import gates

    # The CLI builds and disposes its OWN engine (a process restart), on the same
    # migrated DB — never the shared session engine.
    monkeypatch.setattr(
        clients, "create_engine", lambda _s: create_async_engine(schema_url, poolclass=NullPool)
    )
    monkeypatch.setattr(
        clients, "create_redis", lambda _s: fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    monkeypatch.setattr(
        cli, "build_gateway", lambda _s, *, engine, redis: metered_gateway(engine, tmp_path)
    )

    # Without --confirm the run refuses to touch the database.
    assert cli.main(["benchmark", "run", "--profile", "mock"]) == 2
    assert not (tmp_path / "kpi.json").exists()

    out = tmp_path / "kpi.json"
    saved = gates.snapshot_gates()
    try:
        rc = cli.main(["benchmark", "run", "--profile", "mock", "--confirm", "--output", str(out)])
    finally:
        gates.restore_gates(saved)

    assert rc == 0
    assert out.exists()
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["run_id"]
    assert report["profile"] == "mock"
    assert len(report["per_brief"]) == 10
    assert report["suite"]["crash_recovery_rate"] == 1.0
    assert report["suite"]["autonomous_completion_rate"] == 1.0
    assert report["generated_at"] is not None
    # The report is also echoed to stdout (alongside structured log lines).
    assert '"run_id"' in capsys.readouterr().out
