"""Phase 78 (Vol 9 §12, OPS-025): the runbook-link resolution gate.

The DoD's verifiable clause — a scripted check proves every paging alert's
runbook_url resolves to an existing runbook anchor. These tests pin the live
reconciliation green AND re-break the check so a dangling anchor / stale path /
missing file each go RED (a check that can't fail proves nothing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.check_runbook_links import (
    RULES_DIR,
    RUNBOOKS_DIR,
    _github_slug,
    _runbook_urls,
    collect_errors,
    missing_runbooks,
    runbook_anchors,
)

pytestmark: list[Any] = []

_GH_URL = "https://github.com/titan-platform/titan/blob/main"


# ---- the live repo: every real runbook_url must resolve -----------------------


def test_live_runbook_urls_all_resolve() -> None:
    # non-vacuous: the checker must actually be seeing the real alert URLs (>= the 8
    # paging alerts), not silently collecting zero and passing.
    assert len(_runbook_urls(RULES_DIR)) >= 8
    assert missing_runbooks(RUNBOOKS_DIR) == []
    assert collect_errors(RULES_DIR, RUNBOOKS_DIR) == []


def test_backup_failed_and_audit_anchors_exist_live() -> None:
    # spot-check the two anchors that moved files during reconciliation
    assert "backup-failed" in runbook_anchors(RUNBOOKS_DIR / "backup-restore.md")
    assert "audit-chain-broken" in runbook_anchors(RUNBOOKS_DIR / "incident.md")


# ---- slugify unit: headings map to the anchors the alert URLs use -------------


def test_github_slug_matches_alert_anchors() -> None:
    assert _github_slug("API Fast Burn") == "api-fast-burn"
    assert _github_slug("Backup Failed") == "backup-failed"
    assert _github_slug("Audit Chain Broken") == "audit-chain-broken"
    assert _github_slug("Postgres Lag") == "postgres-lag"
    # punctuation is dropped, spaces collapse to hyphens
    assert _github_slug("RPO / RTO targets (§8.3)") == "rpo--rto-targets-83"


# ---- re-break: build a tiny world and prove each failure mode goes RED --------


def _world(tmp: Path, runbook_body: str, url: str) -> tuple[Path, Path]:
    rules = tmp / "prometheus"
    (rules / "rules").mkdir(parents=True)
    books = tmp / "runbooks"
    books.mkdir()
    for name in ("deploy.md", "rollback.md", "incident.md", "backup-restore.md"):
        (books / name).write_text(runbook_body, encoding="utf-8")
    (rules / "a.yml").write_text(
        "groups:\n  - name: g\n    rules:\n"
        "      - alert: Boom\n        expr: up == 0\n"
        "        labels: {severity: page}\n"
        f'        annotations: {{summary: x, runbook_url: "{url}"}}\n',
        encoding="utf-8",
    )
    return rules, books


def test_good_anchor_resolves(tmp_path: Path) -> None:
    url = f"{_GH_URL}/ops/runbooks/incident.md#boom-section"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert collect_errors(rules, books) == []


def test_dangling_anchor_is_flagged(tmp_path: Path) -> None:
    url = f"{_GH_URL}/ops/runbooks/incident.md#no-such-anchor"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("#no-such-anchor not found" in e for e in collect_errors(rules, books))


def test_stale_docs_path_is_flagged(tmp_path: Path) -> None:
    # a URL pointing outside ops/runbooks/ (the pre-reconciliation shape) must fail
    url = f"{_GH_URL}/docs/runbooks/ops.md#boom-section"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("is not the canonical" in e for e in collect_errors(rules, books))


def test_wrong_branch_is_flagged(tmp_path: Path) -> None:
    # a branch rename (main→master/trunk) would 404 every link — must go RED
    url = (
        "https://github.com/titan-platform/titan/blob/master/ops/runbooks/incident.md#boom-section"
    )
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("is not the canonical" in e for e in collect_errors(rules, books))


def test_wrong_host_or_org_is_flagged(tmp_path: Path) -> None:
    url = "https://example.com/titan-platform/titan/blob/main/ops/runbooks/incident.md#boom-section"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("is not the canonical" in e for e in collect_errors(rules, books))


def test_extra_path_segment_is_flagged(tmp_path: Path) -> None:
    url = f"{_GH_URL}/vendor/ops/runbooks/incident.md#boom-section"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("is not the canonical" in e for e in collect_errors(rules, books))


def test_missing_file_is_flagged(tmp_path: Path) -> None:
    url = f"{_GH_URL}/ops/runbooks/ghost.md#boom-section"
    rules, books = _world(tmp_path, "## Boom Section\n", url)
    assert any("missing file ghost.md" in e for e in collect_errors(rules, books))


def test_missing_required_runbook_is_flagged(tmp_path: Path) -> None:
    _, books = _world(tmp_path, "## x\n", f"{_GH_URL}/ops/runbooks/incident.md#x")
    (books / "rollback.md").unlink()
    assert any("rollback.md is missing" in e for e in missing_runbooks(books))


def test_hash_comment_in_code_fence_is_not_an_anchor(tmp_path: Path) -> None:
    body = "## Real Heading\n\n```bash\n# not a heading, just a shell comment\n```\n"
    anchors = runbook_anchors_from_text(tmp_path, body)
    assert "real-heading" in anchors
    assert "not-a-heading-just-a-shell-comment" not in anchors


def runbook_anchors_from_text(tmp: Path, body: str) -> set[str]:
    p = tmp / "r.md"
    p.write_text(body, encoding="utf-8")
    return runbook_anchors(p)
