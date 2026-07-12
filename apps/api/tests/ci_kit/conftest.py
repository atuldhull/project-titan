"""Fixture workspaces for the constitution CI kit (Phase 45).

Each workspace is a REAL git repository with a base commit on ``main`` and a
feature commit on a branch, because two of the gates (diff_coverage, commit_msg)
read git history — a fake workspace would let them pass vacuously and the
fixtures would prove nothing.

The base commit is a clean, constitution-compliant skeleton. Each fixture then
applies exactly ONE violation in its feature commit, which is what lets the DoD
assert "fails at its own gate, passes every other".
"""

import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from titan.ci_kit.config import KitConfig
from titan.ci_kit.install import install_kit

PACKAGE = "shop"


def git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return result.stdout


@dataclass
class Workspace:
    """A git workspace under test, plus the files its feature commit added."""

    root: Path
    changed: list[str] = field(default_factory=list)

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8", newline="\n")


# --- the clean, compliant baseline -------------------------------------------
#
# Four layers, dependencies pointing inward only; SQLAlchemy confined to
# infrastructure; snake_case throughout; a plural table with an `id` PK and an
# `ix_<table>_<cols>` index. This skeleton must pass every gate — if it does not,
# no violation fixture proves anything, because we could not tell the violation
# apart from the noise.

_BASE_FILES: dict[str, str] = {
    f"src/{PACKAGE}/__init__.py": "",
    f"src/{PACKAGE}/domain/__init__.py": "",
    f"src/{PACKAGE}/domain/order.py": """
        \"\"\"The Order aggregate. Zero framework imports (TITAN-WFL-022).\"\"\"

        from dataclasses import dataclass


        @dataclass(frozen=True)
        class Order:
            id: str
            total_cents: int

            def is_free(self) -> bool:
                return self.total_cents == 0
        """,
    f"src/{PACKAGE}/application/__init__.py": "",
    f"src/{PACKAGE}/application/ports.py": """
        \"\"\"Repository interfaces live in application, per TITAN-WFL-025.\"\"\"

        from typing import Protocol

        from shop.domain.order import Order


        class OrderRepository(Protocol):
            def get(self, order_id: str) -> Order | None: ...
        """,
    f"src/{PACKAGE}/application/get_order.py": """
        from shop.application.ports import OrderRepository
        from shop.domain.order import Order


        def get_order(repository: OrderRepository, order_id: str) -> Order | None:
            return repository.get(order_id)
        """,
    f"src/{PACKAGE}/infrastructure/__init__.py": "",
    f"src/{PACKAGE}/infrastructure/order_repository.py": """
        \"\"\"The ONLY layer allowed to import an ORM (TITAN-WFL-025).\"\"\"

        from sqlalchemy import select

        from shop.domain.order import Order


        class SqlOrderRepository:
            def get(self, order_id: str) -> Order | None:
                _ = select
                return Order(id=order_id, total_cents=0)
        """,
    f"src/{PACKAGE}/interface/__init__.py": "",
    f"src/{PACKAGE}/interface/routes.py": """
        from shop.application.get_order import get_order
        from shop.application.ports import OrderRepository


        def read_order(repository: OrderRepository, order_id: str) -> dict[str, str]:
            order = get_order(repository, order_id)
            return {"id": order.id} if order else {}
        """,
    "migrations/0001_orders.sql": """
        -- Plural table, `id` PK, ix_<table>_<cols> index (§6.2).
        CREATE TABLE orders (
            id UUID PRIMARY KEY,
            customer_id UUID NOT NULL,
            total_cents INTEGER NOT NULL
        );

        CREATE INDEX ix_orders_customer_id ON orders (customer_id);
        """,
}


def _cobertura(files: dict[str, dict[int, tuple[int, int, int]]], project_rate: float) -> str:
    """Build a Cobertura report: {path: {line: (hits, branches_taken, branches_total)}}.

    Hand-built rather than produced by a real coverage run: the gate's contract is
    with the XML, so feeding it exact XML pins exactly what it parses, and keeps
    the fixtures hermetic and fast.
    """
    classes = []
    for path, lines in files.items():
        rendered = []
        for number, (hits, taken, total) in sorted(lines.items()):
            if total:
                rendered.append(
                    f'<line number="{number}" hits="{hits}" branch="true" '
                    f'condition-coverage="{100 * taken // total}% ({taken}/{total})"/>'
                )
            else:
                rendered.append(f'<line number="{number}" hits="{hits}"/>')
        joined = "\n        ".join(rendered)
        classes.append(
            f'    <class filename="{path}">\n      <lines>\n        {joined}\n'
            "      </lines>\n    </class>"
        )
    body = "\n".join(classes)
    return (
        f'<?xml version="1.0" ?>\n<coverage line-rate="{project_rate / 100:.4f}" '
        'branch-rate="0.9">\n  <packages>\n    <package name="shop">\n      <classes>\n'
        f"{body}\n      </classes>\n    </package>\n  </packages>\n</coverage>\n"
    )


def make_workspace(
    tmp_path: Path,
    *,
    feature_files: dict[str, str],
    commit_message: str = "feat(shop): add the discount rule",
    coverage: dict[str, dict[int, tuple[int, int, int]]] | None = None,
    project_rate: float = 92.0,
    baseline: float | None = None,
) -> Workspace:
    """A git workspace: clean base commit on main, feature commit on a branch.

    The kit is installed AFTER the commits on purpose. Installing first would
    activate the commit-msg hook, which would reject the deliberately-bad message
    of the commit_msg fixture and make the fixture uncreatable. The hook is
    exercised end-to-end by its own test instead.
    """
    workspace = Workspace(root=tmp_path)

    for path, content in _BASE_FILES.items():
        workspace.write(path, content)

    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@titan.local")
    git(tmp_path, "config", "user.name", "titan-ci")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "feat(shop): the order skeleton")

    git(tmp_path, "checkout", "-b", "feature")
    for path, content in feature_files.items():
        workspace.write(path, content)
        workspace.changed.append(path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--no-verify", "-m", commit_message)

    install_kit(
        tmp_path,
        KitConfig(package=PACKAGE, src="src", migrations="migrations", base_ref="main"),
    )

    if coverage is None:
        # Default: every added line is covered, so the coverage gate is green and
        # any failure a fixture produces is attributable to its own violation.
        coverage = {
            path: {line: (1, 0, 0) for line in range(1, 40)}
            for path in workspace.changed
            if path.endswith(".py")
        }
    (tmp_path / "coverage.xml").write_text(
        _cobertura(coverage, project_rate), encoding="utf-8", newline="\n"
    )

    if baseline is not None:
        from titan.ci_kit.gates.diff_coverage import write_baseline

        write_baseline(tmp_path, baseline)

    return workspace


@pytest.fixture
def clean_workspace(tmp_path: Path) -> Workspace:
    """A workspace that must pass every constitutional gate."""
    return make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/application/apply_discount.py": """
                from shop.domain.order import Order


                def apply_discount(order: Order, percent: int) -> Order:
                    if percent <= 0:
                        return order
                    reduced = order.total_cents - (order.total_cents * percent // 100)
                    return Order(id=order.id, total_cents=reduced)
                """,
        },
    )
