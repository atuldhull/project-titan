"""DoD: gitleaks over LOG OUTPUT finds nothing — with the SAME engine and
config `make secret-scan` / CI use (the Docker image + the repo's
.gitleaks.toml), run against REAL pipeline-produced log lines.

Built-in re-break (the Phase-66 lesson: a gate nobody has seen fail is
vacuous): the same scan runs over a CONTROL file holding the raw planted
secrets and MUST report leaks — proving the scanner actually bites before we
trust its clean verdict on the redacted logs.

Needs a reachable Docker daemon (pulls zricethezav/gitleaks) — `sandbox`
marker, same non-blocking lane as the container red-team suites.
"""

import json
import logging
import subprocess
from pathlib import Path

import pytest
import structlog

from titan.observability.logging import configure_logging

pytestmark = pytest.mark.sandbox

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GITLEAKS_IMAGE = "zricethezav/gitleaks:latest"

# One planted secret per gitleaks-detectable class (AWS + GitHub are stock
# rules; the age identity is the repo's own titan-age-identity rule).
_PLANTED = (
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWX123456789012",
    "AGE-SECRET-KEY-1" + "Q" * 58,
)


def _docker_ready() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "version"], capture_output=True, timeout=30, check=False
            ).returncode
            == 0
        )
    except Exception:
        return False


def _gitleaks(scan_dir: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{scan_dir}:/scan",
            "-v",
            f"{_REPO_ROOT / '.gitleaks.toml'}:/gitleaks.toml",
            _GITLEAKS_IMAGE,
            "detect",
            "--no-git",
            "--source",
            "/scan",
            "--config",
            "/gitleaks.toml",
            "--no-banner",
        ],
        capture_output=True,
        timeout=300,
        check=False,
    )


def test_gitleaks_finds_nothing_in_redacted_logs_and_bites_on_the_control(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _docker_ready():
        pytest.skip("no reachable Docker daemon")

    # REAL pipeline output: plant every secret through structlog-native AND
    # foreign stdlib records, capture the JSON lines promtail would ship.
    configure_logging(service="titan-test")
    logger = structlog.get_logger("titan.test")
    for secret in _PLANTED:
        logger.info("deploy.env_dump", value=secret, note=f"key={secret} in output")
        logging.getLogger("uvicorn.error").error("boom with %s", secret)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2 * len(_PLANTED)
    for line in lines:
        json.loads(line)  # every captured line is a real pipeline JSON line

    redacted_dir = tmp_path / "redacted"
    redacted_dir.mkdir()
    (redacted_dir / "titan.log").write_text("\n".join(lines), encoding="utf-8")

    control_dir = tmp_path / "control"
    control_dir.mkdir()
    (control_dir / "titan.log").write_text(
        "\n".join(f'{{"event": "deploy.env_dump", "value": "{s}"}}' for s in _PLANTED),
        encoding="utf-8",
    )

    # The control MUST trip the scanner (exit 1 = leaks found) — otherwise the
    # clean verdict below proves nothing.
    control = _gitleaks(control_dir)
    assert control.returncode == 1, (
        f"gitleaks failed to flag the RAW control secrets "
        f"(rc={control.returncode}): {control.stderr.decode(errors='replace')[-2000:]}"
    )

    clean = _gitleaks(redacted_dir)
    assert clean.returncode == 0, (
        f"gitleaks found leaks in REDACTED log output:\n"
        f"{clean.stdout.decode(errors='replace')[-2000:]}\n"
        f"{clean.stderr.decode(errors='replace')[-2000:]}"
    )
