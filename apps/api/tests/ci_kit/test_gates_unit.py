"""Unit tests for the gates' pure cores.

The fixture-workspace suite proves the gates fire on real repos; these pin the
edge cases that a fixture would be a clumsy way to express — mostly the false
positives that would get a gate disabled, and the fail-open paths where a gate
could wave a violation through.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci_kit.conftest import PACKAGE, Workspace, git, make_workspace
from titan.ci_kit.config import (
    MIN_BRANCH_COVERAGE,
    MIN_LINE_COVERAGE,
    ConfigError,
    KitConfig,
    parse_config,
)
from titan.ci_kit.gates import (
    commit_msg,
    diff_coverage,
    layering,
    migration_sql,
    naming,
    orm_boundary,
)
from titan.ci_kit.gates.diff_coverage import CoverageError, DiffCoverage
from titan.ci_kit.gates.runner import run_gates
from titan.ci_kit.install import install_kit
from titan.ci_kit.render import KIT_ROOT, render_kit

# --- TITAN-WFL-025: the ORM boundary -----------------------------------------


def test_orm_imports_finds_every_import_form() -> None:
    source = (
        "import sqlalchemy\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "import asyncpg\n"
    )
    assert [module for _, module in orm_boundary.orm_imports(source)] == [
        "sqlalchemy",
        "sqlalchemy",
        "sqlalchemy.orm",
        "asyncpg",
    ]


def test_orm_imports_ignores_comments_and_strings() -> None:
    """The §6.1 acceptance test says "grep gate"; a literal grep fires on both of
    these. A gate with false positives is a gate the team turns off."""
    source = '# do not import sqlalchemy here\nNAME = "sqlalchemy"\nDOCS = """sqlalchemy"""\n'
    assert orm_boundary.orm_imports(source) == []


def test_orm_imports_ignores_lookalike_packages() -> None:
    # `sqlalchemy_utils` is a different distribution; matching on a prefix would
    # flag it, and matching on a substring would flag `my_sqlalchemy_helpers`.
    assert orm_boundary.orm_imports("import sqlalchemy_utils\n") == []


def test_orm_imports_survives_a_syntax_error() -> None:
    # Ruff and mypy own syntax errors. This gate must not report "you imported
    # SQLAlchemy" because a paren is unbalanced.
    assert orm_boundary.orm_imports("def broken(:\n") == []


def test_orm_imports_ignores_relative_imports() -> None:
    assert orm_boundary.orm_imports("from . import sqlalchemy\n") == []


# --- TITAN-WFL-027: Conventional Commits -------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "feat(billing): generate invoice PDFs",
        "fix: handle the empty cart",
        "feat!: drop the v1 endpoint",
        "chore(ci): bump ruff",
        "feat(billing): generate PDFs\n\nA body explaining why.",
        "Merge branch 'main' into feature",
        'Revert "feat: a bad idea"',
        "fixup! feat: the original",
    ],
)
def test_conventional_commits_accepted(message: str) -> None:
    assert commit_msg.check_message(message).ok, message


@pytest.mark.parametrize(
    ("message", "why"),
    [
        ("updated some stuff", "no type prefix"),
        ("", "empty"),
        ("feat generate PDFs", "missing colon"),
        ("Feat: generate PDFs", "type must be lowercase"),
        ("wip: something", "'wip' is not a Conventional Commits type"),
        ("feat(billing): generate PDFs.", "trailing period"),
        ("feat(billing): " + "x" * 80, "subject over 72 chars"),
        ("feat: subject\nbody with no blank line", "body not separated"),
    ],
)
def test_non_conventional_commits_rejected(message: str, why: str) -> None:
    outcome = commit_msg.check_message(message)
    assert not outcome.ok, why
    assert outcome.findings[0].wfl_id == "TITAN-WFL-027"
    assert outcome.findings[0].fix


def test_commit_msg_ignores_git_comment_lines() -> None:
    # git appends its own '#'-prefixed scaffolding to COMMIT_EDITMSG.
    message = "feat(shop): add discounts\n\n# Please enter the commit message\n# On branch main\n"
    assert commit_msg.check_message(message).ok


# --- TITAN-WFL-027: the §6.2 database naming row -----------------------------


def test_migration_lint_accepts_a_compliant_migration() -> None:
    sql = """
    CREATE TABLE invoices (
        id UUID PRIMARY KEY,
        customer_id UUID NOT NULL REFERENCES customers (id)
    );
    CREATE INDEX ix_invoices_customer_id ON invoices (customer_id);
    """
    assert migration_sql.lint_sql(sql, "m.sql") == []


def test_migration_lint_rejects_singular_table_and_bad_index() -> None:
    sql = """
    CREATE TABLE invoice (
        id UUID PRIMARY KEY
    );
    CREATE INDEX invoice_idx ON invoice (id);
    """
    messages = " ".join(f.message for f in migration_sql.lint_sql(sql, "m.sql"))
    assert "singular" in messages
    assert "ix_invoice_" in messages


def test_migration_lint_rejects_a_table_with_no_id_pk() -> None:
    sql = "CREATE TABLE orders (order_uuid UUID PRIMARY KEY);"
    messages = " ".join(f.message for f in migration_sql.lint_sql(sql, "m.sql"))
    assert "'id'" in messages


def test_migration_lint_ignores_ddl_inside_comments_and_strings() -> None:
    """An Alembic migration's docstring routinely quotes the DDL it applies."""
    sql = '''
    """CREATE TABLE bad_singular_thing (name TEXT);"""
    -- CREATE TABLE another_bad_one (name TEXT);
    /* CREATE INDEX wrong_name ON stuff (x); */
    CREATE TABLE orders (id UUID PRIMARY KEY);
    '''
    assert migration_sql.lint_sql(sql, "m.sql") == []


def test_migration_lint_does_not_guess_at_irregular_plurals() -> None:
    # 'status' and 'addresses' end in 's'; a naive singular check flags the first.
    sql = "CREATE TABLE addresses (id UUID PRIMARY KEY);"
    assert migration_sql.lint_sql(sql, "m.sql") == []


def test_strip_noise_preserves_line_numbers() -> None:
    sql = "-- a comment\n/* two\n   lines */\nCREATE TABLE orders (id UUID PRIMARY KEY);\n"
    assert migration_sql.strip_noise(sql).count("\n") == sql.count("\n")
    findings = migration_sql.lint_sql(sql, "m.sql")
    assert findings == []


def test_strip_noise_handles_dollar_quoting() -> None:
    """A lone apostrophe inside a $$...$$ body must not desync the scanner and
    swallow the real DDL that follows — dollar-quoting is ubiquitous in Postgres
    function bodies."""
    sql = (
        "CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN "
        "RAISE EXCEPTION 'it''s append-only'; END; $$ LANGUAGE plpgsql;\n"
        "CREATE TABLE orders (id UUID PRIMARY KEY);\n"
    )
    cleaned = migration_sql.strip_noise(sql)
    assert cleaned.count("\n") == sql.count("\n")
    # The CREATE TABLE after the function body must survive the blanking.
    assert "CREATE TABLE orders" in cleaned
    assert migration_sql.lint_sql(sql, "m.sql") == []


def test_dollar_quote_hides_ddl_inside_a_function_body() -> None:
    # A CREATE TABLE quoted inside a $tag$...$tag$ body is not real DDL.
    sql = (
        "CREATE FUNCTION g() RETURNS void AS $body$ "
        "CREATE TABLE bad_singular (name text); $body$ LANGUAGE sql;\n"
    )
    assert migration_sql.lint_sql(sql, "m.sql") == []


def test_singular_table_still_caught_after_a_dollar_quoted_function() -> None:
    sql = (
        "CREATE FUNCTION t() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql;\n"
        "CREATE TABLE invoice (id UUID PRIMARY KEY);\n"
    )
    messages = " ".join(f.message for f in migration_sql.lint_sql(sql, "m.sql"))
    assert "singular" in messages


# --- TITAN-WFL-030: the coverage gates ---------------------------------------


def test_parse_diff_takes_added_lines_only() -> None:
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,0 +2,3 @@\n"
        "+one\n+two\n+three\n"
    )
    assert diff_coverage.parse_diff(diff) == {"src/a.py": {2, 3, 4}}


def test_parse_diff_ignores_pure_deletions() -> None:
    # A deleted file has no lines to cover; counting them would fail every
    # cleanup PR at 0% coverage.
    diff = (
        "diff --git a/src/gone.py b/src/gone.py\n"
        "--- a/src/gone.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
    )
    assert diff_coverage.parse_diff(diff) == {}


def test_parse_diff_does_not_mistake_a_plusplus_content_line_for_a_header() -> None:
    """An added line whose content starts with '++ ' renders as '+++ ' under
    --unified=0; reading it as a file header would redirect every later added
    line to a bogus path and silently drop the real file's coverage."""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,0 +1,2 @@\n"
        "+x = 1\n"
        "++ this is content, not a header\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -1,0 +5,1 @@\n"
        "+y = 2\n"
    )
    # a.py's lines come from the @@ header (1,2); b.py from its own @@ (5). The
    # '++ ' content line must NOT have become a header for a file named
    # '+ this is content...'.
    assert diff_coverage.parse_diff(diff) == {"src/a.py": {1, 2}, "src/b.py": {5}}


def test_measure_ignores_files_absent_from_the_coverage_report() -> None:
    # README.md is in the diff and not in coverage.xml. A docs-only PR must not
    # fail the coverage gate at 0%.
    changed = {"README.md": {1, 2, 3}}
    xml = '<coverage line-rate="0.9"><packages></packages></coverage>'
    result = diff_coverage.measure(changed, xml)
    assert result.lines_total == 0
    assert result.line_rate == 100.0


def test_measure_reconstructs_source_relative_filenames() -> None:
    """coverage.py with [tool.coverage.run] source=["src/pkg"] emits filenames
    RELATIVE to the source root; the gate must prefix <source> to match a
    git-diff path — else every file reads as 'no coverage data' (a real fail-open
    the Phase-46 scaffold hit)."""
    xml = (
        '<coverage line-rate="0.95">'
        "<sources><source>src/billing</source></sources>"
        "<packages><package><classes>"
        '<class filename="application/fees.py"><lines>'
        '<line number="1" hits="1"/><line number="2" hits="1"/>'
        "</lines></class></classes></package></packages></coverage>"
    )
    # The git diff names the file from the repo root.
    result = diff_coverage.measure({"src/billing/application/fees.py": {1, 2}}, xml)
    assert result.lines_total == 2
    assert result.lines_covered == 2
    assert result.line_rate == 100.0


def test_absolute_source_root_matches_by_suffix() -> None:
    xml = (
        '<coverage line-rate="0.9">'
        "<sources><source>/runner/work/repo/src/billing</source></sources>"
        "<packages><package><classes>"
        '<class filename="application/fees.py"><lines><line number="1" hits="0"/></lines></class>'
        "</classes></package></packages></coverage>"
    )
    result = diff_coverage.measure({"src/billing/application/fees.py": {1}}, xml)
    assert result.lines_total == 1
    assert result.lines_covered == 0


def test_measure_counts_branches_on_changed_lines() -> None:
    xml = (
        '<coverage line-rate="0.9"><packages><package><classes>'
        '<class filename="src/a.py"><lines>'
        '<line number="1" hits="1"/>'
        '<line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/>'
        "</lines></class></classes></package></packages></coverage>"
    )
    result = diff_coverage.measure({"src/a.py": {1, 2}}, xml)
    assert result.lines_total == 2
    assert result.lines_covered == 2
    assert result.branches_total == 2
    assert result.branches_taken == 1
    assert result.branch_rate == 50.0


def test_full_line_coverage_with_weak_branch_coverage_still_fails() -> None:
    """§6.5 sets a branch gate precisely because line coverage alone is satisfied
    by a test that executes an `if` and never takes the false edge."""
    result = DiffCoverage(
        lines_total=10,
        lines_covered=10,  # 100% line
        branches_total=10,
        branches_taken=5,  # 50% branch, below the 70% gate
        project_rate=95.0,
    )
    assert result.line_rate == 100.0
    outcome = diff_coverage.evaluate(result, baseline=None)
    assert not outcome.ok
    assert "branch coverage" in outcome.findings[0].message


def test_thresholds_are_the_constitutional_floors() -> None:
    assert (MIN_LINE_COVERAGE, MIN_BRANCH_COVERAGE) == (80.0, 70.0)

    at_the_floor = DiffCoverage(
        lines_total=10, lines_covered=8, branches_total=10, branches_taken=7, project_rate=90.0
    )
    assert diff_coverage.evaluate(at_the_floor, baseline=None).ok, ">= is inclusive"

    below = DiffCoverage(
        lines_total=100, lines_covered=79, branches_total=10, branches_taken=7, project_rate=90.0
    )
    assert not diff_coverage.evaluate(below, baseline=None).ok


def test_baseline_is_not_ratcheted_down_by_a_failing_run(tmp_path: Path) -> None:
    """Writing the floor on a FAILING run would record the just-rejected rate as
    the new floor, so the next PR could re-fail at the lower number and pass."""
    path = f"src/{PACKAGE}/application/thing.py"
    workspace = make_workspace(
        tmp_path,
        feature_files={path: "def thing() -> int:\n    return 1\n"},
        coverage={path: {1: (0, 0, 0), 2: (0, 0, 0)}},  # 0% diff coverage -> FAIL
        project_rate=40.0,
        baseline=88.0,
    )
    outcome = diff_coverage.check(workspace.root, update_baseline=True)
    assert not outcome.ok
    assert diff_coverage.read_baseline(workspace.root) == 88.0, "the floor must not fall"


def test_a_corrupt_baseline_errors_rather_than_disabling_the_rule(
    clean_workspace: Workspace,
) -> None:
    (clean_workspace.root / diff_coverage.BASELINE_PATH).write_text("{ not json", encoding="utf-8")
    with pytest.raises(CoverageError):
        diff_coverage.check(clean_workspace.root)


def test_passing_run_records_the_new_floor(clean_workspace: Workspace) -> None:
    outcome = diff_coverage.check(clean_workspace.root, update_baseline=True)
    assert outcome.ok
    assert diff_coverage.read_baseline(clean_workspace.root) == pytest.approx(92.0)


def test_untested_new_source_file_absent_from_report_fails(tmp_path: Path) -> None:
    """The blocking fail-open: coverage.py OMITS a source file no test executed
    (rather than listing it at 0%), so a brand-new untested module vanishes from
    the report. It must FAIL, not skip through as "no changed lines"."""
    path = f"src/{PACKAGE}/application/untested.py"
    workspace = make_workspace(
        tmp_path,
        feature_files={path: "def compute(x: int) -> int:\n    return x * 2\n"},
        # The report does NOT mention the new file — exactly what coverage.py does
        # for an unexecuted module when no `source` is configured.
        coverage={f"src/{PACKAGE}/domain/order.py": {1: (1, 0, 0)}},
        baseline=None,
    )
    outcome = diff_coverage.check(workspace.root)
    assert not outcome.ok, "an untested new source module must fail the diff-coverage gate"
    assert any("no coverage data" in f.message and "untested" in f.path for f in outcome.findings)


def test_untested_source_check_ignores_empty_init_and_docs(tmp_path: Path) -> None:
    """The absent-from-report check must not false-fail files with nothing to
    cover: an empty __init__.py, a docstring-only module, or a README."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/application/sub/__init__.py": "",
            f"src/{PACKAGE}/application/notes.py": '"""Just a docstring."""\n',
            "README.md": "# docs\n",
        },
        coverage={f"src/{PACKAGE}/domain/order.py": {1: (1, 0, 0)}},
        baseline=70.0,  # project_rate default 92.0 >= 70.0, never-decrease holds
    )
    outcome = diff_coverage.check(workspace.root)
    assert outcome.ok, outcome.render()


def test_never_decrease_runs_even_with_no_changed_executable_lines(tmp_path: Path) -> None:
    """The blocking bug: the old `lines_total == 0 -> skipped` short-circuit ran
    BEFORE the never-decrease check, so a PR touching only non-source files (e.g.
    deleting a test) could drop project coverage and pass. Never-decrease is
    independent of the diff and must always run."""
    workspace = make_workspace(
        tmp_path,
        feature_files={"docs/guide.md": "# a docs-only change\n"},
        coverage={f"src/{PACKAGE}/domain/order.py": {1: (1, 0, 0)}},
        project_rate=60.0,
        baseline=88.0,  # coverage fell 88 -> 60 with no changed source lines
    )
    outcome = diff_coverage.check(workspace.root)
    assert not outcome.ok, "never-decrease must fire even when the diff has no source lines"
    assert any("never decrease" in f.fix for f in outcome.findings)


def test_docs_only_change_passes_when_the_floor_holds(tmp_path: Path) -> None:
    workspace = make_workspace(
        tmp_path,
        feature_files={"README.md": "# just docs\n"},
        coverage={f"src/{PACKAGE}/domain/order.py": {1: (1, 0, 0)}},
        project_rate=90.0,
        baseline=88.0,  # coverage did not fall
    )
    assert diff_coverage.check(workspace.root).ok


# --- TITAN-WFL-027: Ruff naming output ---------------------------------------


def test_naming_gate_ignores_workspace_ruff_opt_out(tmp_path: Path) -> None:
    """A project must not be able to opt out of the naming gate by adding an
    ignore to its own ruff.toml — the gate runs ruff --isolated for exactly that
    reason (the gate-loosening the whole kit exists to prevent)."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/application/bad.py": ("def camelCaseName() -> int:\n    return 1\n"),
        },
    )
    # The project tries to suppress pep8-naming for its whole package.
    (workspace.root / "ruff.toml").write_text(
        '[lint.per-file-ignores]\n"src/**" = ["N"]\n', encoding="utf-8"
    )
    outcome = naming.check(workspace.root)
    assert not outcome.ok, "the workspace ruff.toml must not disable the naming gate"
    assert any("N802" in f.message for f in outcome.findings)


# --- TITAN-WFL-022/027: the shared ESLint runner (TypeScript) ----------------


def test_parse_eslint_unix_extracts_rule_and_location() -> None:
    from titan.ci_kit.gates._eslint import parse_eslint_unix

    output = (
        "/ws/src/a.ts:12:5: 'x' unused [Error/@typescript-eslint/no-unused-vars]\n"
        "/ws/src/a.ts:20:1: Unexpected console statement [Error/no-console]\n"
        "/ws/src/b.ts:3:10: Disallowed import [Error/boundaries/element-types]\n"
        "\n3 problems\n"
    )
    violations = parse_eslint_unix(output)
    assert [(v.line, v.rule) for v in violations] == [
        (12, "@typescript-eslint/no-unused-vars"),
        (20, "no-console"),
        (3, "boundaries/element-types"),
    ]


def test_eslint_fail_closed_when_it_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ESLint exit with no parseable violation is a tool failure and
    must surface as an ERROR from both TS gates, never a silent pass."""
    from titan.ci_kit.gates import _eslint

    workspace = make_workspace(tmp_path, feature_files={"NOTES.md": "# notes\n"})
    # Declare a TS source dir so the TS path runs.
    (workspace.root / "ts").mkdir()
    config = workspace.root / ".titan-ci.toml"
    config.write_text(config.read_text(encoding="utf-8") + 'ts_src = "ts"\n', encoding="utf-8")

    def _boom(_ws: Path, _ts: str) -> list[object]:
        raise _eslint.EslintUnavailable("eslint could not resolve its config")

    monkeypatch.setattr(_eslint, "run_eslint", _boom)
    monkeypatch.setattr(layering, "run_eslint", _boom)
    monkeypatch.setattr(naming, "run_eslint", _boom)

    with pytest.raises(layering.GateUnavailable):
        layering.check(workspace.root)
    with pytest.raises(naming.GateUnavailable):
        naming.check(workspace.root)


def test_layering_and_naming_partition_eslint_rules() -> None:
    """boundaries/* is the layering gate's; camelcase is the naming gate's. The
    two must not both claim the same rule, or a violation double-counts."""
    from titan.ci_kit.gates.naming import _NAMING_RULES

    assert "camelcase" in _NAMING_RULES
    assert not any(r.startswith("boundaries") for r in _NAMING_RULES)


def test_parse_ruff_keeps_only_naming_codes() -> None:
    output = "src/a.py:3:5: N802 Function name `applyDiscount` should be lowercase\n"
    findings = naming.parse_ruff(output)
    assert len(findings) == 1
    assert findings[0].line == 3
    assert "N802" in findings[0].message


# --- config + install --------------------------------------------------------


def test_config_requires_a_package() -> None:
    with pytest.raises(ConfigError):
        parse_config("[titan-ci]\nsrc = 'src'\n")
    with pytest.raises(ConfigError):
        parse_config("not toml {{{")


def test_install_is_idempotent_and_preserves_project_owned_files(tmp_path: Path) -> None:
    config = KitConfig(package="shop")
    install_kit(tmp_path, config)

    # A project may legitimately add its own rules to ruff.toml.
    (tmp_path / "ruff.toml").write_text("# project's own rules\n", encoding="utf-8")
    result = install_kit(tmp_path, config)

    assert "ruff.toml" in result.skipped
    assert (tmp_path / "ruff.toml").read_text(encoding="utf-8") == "# project's own rules\n"

    # ...but the gates themselves are the kit's, and a stale copy must not survive
    # a kit upgrade.
    gate = tmp_path / KIT_ROOT / "titan" / "ci_kit" / "gates" / "orm_boundary.py"
    gate.write_text("# stale\n", encoding="utf-8")
    install_kit(tmp_path, config)
    assert "stale" not in gate.read_text(encoding="utf-8")

    forced = install_kit(tmp_path, config, force=True)
    assert "ruff.toml" in forced.written


def test_the_installed_kit_is_importable_on_its_own(tmp_path: Path) -> None:
    """The vendored copy must run with a bare python3 and PYTHONPATH=.titan/ci —
    that is the whole contract with the network-denied sandbox and with the
    generated project's CI, neither of which can pip-install the kit."""
    install_kit(tmp_path, KitConfig(package="shop"))

    # A pristine env with ONLY PYTHONPATH=.titan/ci — no editable install of
    # titan on the path, which is what proves the vendored copy stands alone.
    env = dict(os.environ)
    env["PYTHONPATH"] = KIT_ROOT
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [sys.executable, "-m", "titan.ci_kit.gates.runner", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "constitutional" in result.stdout.lower()

    # Prove it was the VENDORED copy that ran, not the editable install that
    # happens to be on this venv's path — otherwise the test above would pass
    # even if the vendored tree were empty.
    resolved = subprocess.run(
        [sys.executable, "-c", "import titan.ci_kit as k; print(k.__file__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=env,
        timeout=120,
    )
    assert KIT_ROOT.replace("/", os.sep) in resolved.stdout


def test_a_typo_in_the_package_name_errors_rather_than_skipping(
    clean_workspace: Workspace,
) -> None:
    """A declared package that does not exist is a misconfiguration.

    If the Python gates SKIPPED on a missing package root, one wrong character in
    `.titan-ci.toml` would silently disable layering, naming and orm_boundary at
    once — three gates green, nothing checked.
    """
    config = clean_workspace.root / ".titan-ci.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace('package = "shop"', 'package = "shopp"'),
        encoding="utf-8",
    )

    reports = {report.gate: report for report in run_gates(clean_workspace.root)}
    for gate in ("layering", "naming", "orm_boundary"):
        assert not reports[gate].ok, f"{gate} must not pass when its package root is missing"
        assert reports[gate].outcome is None, f"{gate} must ERROR, not FAIL"
        assert "does not exist" in reports[gate].error


def test_render_is_pure_and_covers_every_declared_gate() -> None:
    files = {file.path for file in render_kit(KitConfig(package="shop"))}
    assert ".titan-ci.toml" in files
    assert ".importlinter" in files
    assert "ruff.toml" in files
    assert "eslint.config.mjs" in files
    assert ".githooks/commit-msg" in files
    assert ".github/workflows/constitution.yml" in files
    # The kit vendors itself so the workspace can run the gates without pip.
    assert f"{KIT_ROOT}/titan/ci_kit/gates/runner.py" in files


def test_commit_msg_hook_actually_rejects_a_bad_commit(tmp_path: Path) -> None:
    """End-to-end: install the kit, then try to make a non-conventional commit.

    The fixture workspaces commit with --no-verify (they must be able to CREATE a
    bad-message commit), so without this test the hook itself — as opposed to the
    CI-side check of the same rule — would never be executed.
    """
    install_kit(tmp_path, KitConfig(package="shop"))
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@titan.local")
    git(tmp_path, "config", "user.name", "titan-ci")
    git(tmp_path, "config", "core.hooksPath", ".githooks")
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")
    git(tmp_path, "add", "-A")

    rejected = subprocess.run(
        ["git", "commit", "-m", "updated some stuff"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if "python3" in rejected.stderr and "not found" in rejected.stderr:
        pytest.skip("the hook needs python3 on PATH (it has it in the sandbox image and in CI)")

    assert rejected.returncode != 0, "the commit-msg hook must reject a non-conventional subject"
    assert "TITAN-WFL-027" in rejected.stdout + rejected.stderr

    accepted = subprocess.run(
        ["git", "commit", "-m", "feat(shop): add the thing"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
