"""CI gate: every Prometheus alert ``runbook_url`` resolves to a real runbook anchor.

Phase 78 (Vol 9 §12, TITAN-OPS-025). The DoD is "a scripted check proves every paging
alert's runbook_url resolves to an existing runbook anchor." This walks every alert rule
under ``infra/prometheus/`` (root + ``rules/``), pulls each ``annotations.runbook_url``,
and asserts it is the canonical repo blob URL
``https://github.com/titan-platform/titan/blob/main/ops/runbooks/<file>.md#<anchor>``
where the file exists and some heading in it GitHub-slugifies to ``<anchor>``. Any other
host/org/branch (a `main`→rename would silently break every link), a stale
``docs/runbooks/...`` path, an extra path segment, a missing file, or a dangling anchor
is a hard failure. A zero-URL collection (rules renamed/moved) is also a failure — the
gate must never pass vacuously.

Also asserts the four canonical runbooks (deploy/rollback/incident/backup-restore) exist.

Pure filesystem + YAML — no DB, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
RULES_DIR = _ROOT / "infra" / "prometheus"
RUNBOOKS_DIR = _ROOT / "ops" / "runbooks"

# The four runbooks that MUST exist as executable documents (Vol 9 §12).
_REQUIRED_RUNBOOKS = ("deploy.md", "rollback.md", "incident.md", "backup-restore.md")

# A runbook_url MUST be the canonical repo blob URL for a file+anchor under ops/runbooks/.
# Anchoring the whole URL (not just the path tail) catches a wrong host/org, a branch
# rename (main→…), or an extra path segment — any of which 404s for a paged operator.
_CANONICAL = "https://github.com/titan-platform/titan/blob/main/ops/runbooks/"
_RUNBOOK_URL = re.compile(r"^" + re.escape(_CANONICAL) + r"([^/#\s]+\.md)#(\S+)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _github_slug(heading: str) -> str:
    """Reproduce GitHub's heading→anchor slug: lowercase, drop punctuation (keeping
    word chars, spaces, hyphens), then spaces→hyphens. Matches how the alert URLs'
    ``#fragment`` is generated from a Markdown heading."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s, flags=re.UNICODE)  # \w keeps letters/digits/underscore
    return s.replace(" ", "-")


def runbook_anchors(md_path: Path) -> set[str]:
    """Every anchor GitHub would mint for ``md_path``'s headings, with the same
    ``-1``/``-2`` disambiguation for repeated slugs — and skipping fenced code blocks
    so a ``# comment`` line inside a shell block is not mistaken for a heading."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        base = _github_slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        anchors.add(base if n == 0 else f"{base}-{n}")
    return anchors


def _runbook_urls(rules_dir: Path) -> list[tuple[str, str, str]]:
    """(source_file, alert_name, runbook_url) for every alert carrying one."""
    files = sorted(rules_dir.glob("*.yml")) + sorted((rules_dir / "rules").glob("*.yml"))
    out: list[tuple[str, str, str]] = []
    for path in files:
        if not path.is_file():
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                url = (rule.get("annotations") or {}).get("runbook_url")
                if url:
                    out.append((path.name, rule.get("alert", "<record>"), str(url)))
    return out


def collect_errors(rules_dir: Path, runbooks_dir: Path) -> list[str]:
    errors: list[str] = []
    anchors_cache: dict[Path, set[str]] = {}
    for fname, alert, url in _runbook_urls(rules_dir):
        m = _RUNBOOK_URL.match(url)
        if not m:
            errors.append(
                f"{fname}: alert {alert} runbook_url is not the canonical "
                f"{_CANONICAL}<file>.md#<anchor>: {url}"
            )
            continue
        target = runbooks_dir / m.group(1)
        anchor = m.group(2)
        if not target.is_file():
            errors.append(f"{fname}: alert {alert} runbook_url points at missing file {m.group(1)}")
            continue
        anchors = anchors_cache.setdefault(target, runbook_anchors(target))
        if anchor not in anchors:
            errors.append(
                f"{fname}: alert {alert} runbook_url anchor #{anchor} not found in "
                f"{m.group(1)} (have: {', '.join(sorted(anchors)) or 'none'})"
            )
    return errors


def missing_runbooks(runbooks_dir: Path) -> list[str]:
    return [
        f"required runbook ops/runbooks/{name} is missing"
        for name in _REQUIRED_RUNBOOKS
        if not (runbooks_dir / name).is_file()
    ]


def main() -> None:
    urls = _runbook_urls(RULES_DIR)
    if not urls:
        # Never pass vacuously: zero URLs means the rules moved/renamed out of reach
        # (the sibling check_alert_rules.py guards its file discovery the same way).
        raise SystemExit(f"check-runbook-links: no runbook_urls found under {RULES_DIR}")
    errors = missing_runbooks(RUNBOOKS_DIR) + collect_errors(RULES_DIR, RUNBOOKS_DIR)
    if errors:
        sys.stderr.write("runbook-links check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        f"runbook-links OK: {len(urls)} alert runbook_url(s) all resolve to an existing "
        f"ops/runbooks anchor.\n"
    )


if __name__ == "__main__":
    main()
