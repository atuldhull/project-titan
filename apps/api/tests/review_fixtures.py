"""Prompt-regression fixture PRs for the §6.6 rubric (Phase 47).

One fixture per rule R1-R11, each an otherwise-clean PR that violates EXACTLY its
target rule (so the regression asserts the reviewer cites the right code and
nothing else), plus clean PRs the reviewer must approve. R10/R11 are `major`, so
their fixtures carry two violations — proving both the rule AND the §6.6
"two majors equal one blocking" aggregation drives a REJECTED verdict.

Stored as data (runnable as a suite) rather than on-disk trees so the corpus is
diffable in one place; each fixture is a real code snippet in the shape the rule
targets.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.review.models import ChangedFile, PullRequest

# A docstringed, snake_case, tested helper included in non-R2/R11 fixtures so the
# ONLY violation is the one under test.
_CLEAN_TEST = ChangedFile(
    path="tests/test_widget.py",
    content='"""Widget tests."""\n\n\ndef test_it_works() -> None:\n    assert True\n',
)


def _pr(
    *files: ChangedFile, openapi_changed: bool = False, adr_paths: tuple[str, ...] = ()
) -> PullRequest:
    return PullRequest(files=files, openapi_changed=openapi_changed, adr_paths=adr_paths)


@dataclass(frozen=True)
class Fixture:
    name: str
    expected_rule: str | None  # None => a clean PR (no rejection)
    pr: PullRequest
    note: str = ""


# --- R1: domain -> infrastructure import -------------------------------------
_R1 = Fixture(
    "R1_domain_imports_infrastructure",
    "R1",
    _pr(
        ChangedFile(
            path="src/shop/domain/pricing.py",
            content=(
                '"""Pricing rules."""\n\n'
                "from shop.infrastructure.repo import PriceRepo\n\n\n"
                "def price(repo: PriceRepo) -> int:\n"
                '    """Return a price via the repo."""\n'
                "    return repo.get()\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R2: source with no test -------------------------------------------------
_R2 = Fixture(
    "R2_source_without_tests",
    "R2",
    _pr(
        ChangedFile(
            path="src/shop/application/checkout.py",
            content=(
                '"""Checkout use case."""\n\n\n'
                "def checkout(total: int) -> int:\n"
                '    """Apply tax and return the payable total."""\n'
                "    return total + total // 10\n"
            ),
        ),
    ),
)

# --- R3: hardcoded secret ----------------------------------------------------
_R3 = Fixture(
    "R3_hardcoded_secret",
    "R3",
    _pr(
        ChangedFile(
            path="src/shop/application/client.py",
            content=(
                '"""Payment client."""\n\n'
                'STRIPE_KEY = "sk-live-abcd1234efgh5678ijkl"\n\n\n'
                "def charge() -> str:\n"
                '    """Charge using the configured key."""\n'
                "    return STRIPE_KEY\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R4: duplicated block (>10 lines) ----------------------------------------
_DUP = "\n".join(f"    total_{i} = base * {i} + offset" for i in range(12))
_R4 = Fixture(
    "R4_duplicated_logic",
    "R4",
    _pr(
        ChangedFile(
            path="src/shop/application/report.py",
            content=(
                '"""Report builder."""\n\n\n'
                "def build_a(base: int, offset: int) -> int:\n"
                '    """First variant."""\n'
                f"{_DUP}\n"
                "    return total_11\n\n\n"
                "def build_b(base: int, offset: int) -> int:\n"
                '    """Second variant — a copy-paste of build_a."""\n'
                f"{_DUP}\n"
                "    return total_11\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R5: swallowed exception -------------------------------------------------
_R5 = Fixture(
    "R5_swallowed_exception",
    "R5",
    _pr(
        ChangedFile(
            path="src/shop/application/loader.py",
            content=(
                '"""Loader."""\n\n\n'
                "def load(source: str) -> str:\n"
                '    """Load a source, ignoring failures (the bug)."""\n'
                "    try:\n"
                "        return source.strip()\n"
                "    except Exception:\n"
                "        pass\n"
                '    return ""\n'
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R6: CQRS split with no ADR ----------------------------------------------
_R6 = Fixture(
    "R6_cqrs_without_adr",
    "R6",
    _pr(
        ChangedFile(
            path="src/shop/application/commands/place_order.py",
            content=(
                '"""Place-order command (CQRS write side)."""\n\n\n'
                "def place_order(order_id: str) -> None:\n"
                '    """Place the order."""\n'
                "    return None\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R7: placeholder (TODO + NotImplementedError stub) -----------------------
_R7 = Fixture(
    "R7_placeholder_code",
    "R7",
    _pr(
        ChangedFile(
            path="src/shop/application/refund.py",
            content=(
                '"""Refund use case."""\n\n\n'
                "def refund(order_id: str) -> None:\n"
                '    """Refund an order."""\n'
                "    # TODO: implement the refund flow\n"
                "    raise NotImplementedError\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R8: route change without OpenAPI update ---------------------------------
_R8 = Fixture(
    "R8_api_change_without_openapi",
    "R8",
    _pr(
        ChangedFile(
            path="src/shop/interface/routes.py",
            content=(
                '"""HTTP routes."""\n\n'
                "from fastapi import APIRouter\n\n"
                "router = APIRouter()\n\n\n"
                '@router.get("/widgets/{widget_id}")\n'
                "async def read_widget(widget_id: str) -> dict[str, str]:\n"
                '    """Read a widget."""\n'
                '    return {"id": widget_id}\n'
            ),
        ),
        _CLEAN_TEST,
        openapi_changed=False,
    ),
)

# --- R9: N+1 query in a loop -------------------------------------------------
_R9 = Fixture(
    "R9_n_plus_one",
    "R9",
    _pr(
        ChangedFile(
            path="src/shop/infrastructure/orders.py",
            content=(
                '"""Order reads."""\n\n\n'
                "async def totals(conn: object, ids: list[str]) -> list[int]:\n"
                '    """Sum each order (the N+1 bug)."""\n'
                "    out = []\n"
                "    for oid in ids:\n"
                "        row = await conn.execute(oid)\n"
                "        out.append(row)\n"
                "    return out\n"
            ),
        ),
        _CLEAN_TEST,
    ),
)

# --- R10: two naming violations (2 majors => REJECTED) -----------------------
_R10 = Fixture(
    "R10_naming_violations",
    "R10",
    _pr(
        ChangedFile(
            path="src/shop/application/naming.py",
            content=(
                '"""Naming demo."""\n\n\n'
                "def applyDiscount(x: int) -> int:\n"
                '    """Bad: camelCase function."""\n'
                "    return x\n\n\n"
                "def computeTax(x: int) -> int:\n"
                '    """Bad: camelCase function."""\n'
                "    return x\n"
            ),
        ),
        _CLEAN_TEST,
    ),
    note="two majors == one blocking condition",
)

# --- R11: two missing docstrings (2 majors => REJECTED) ----------------------
_R11 = Fixture(
    "R11_missing_docstrings",
    "R11",
    _pr(
        ChangedFile(
            path="src/shop/application/undocumented.py",
            content=(
                '"""Module header present; the public functions are not documented."""\n\n\n'
                "def surcharge(x: int) -> int:\n"
                "    return x + 1\n\n\n"
                "def rebate(x: int) -> int:\n"
                "    return x - 1\n"
            ),
        ),
        _CLEAN_TEST,
    ),
    note="two majors == one blocking condition",
)

# --- clean PRs ---------------------------------------------------------------
_CLEAN_1 = Fixture(
    "clean_use_case_with_tests",
    None,
    _pr(
        ChangedFile(
            path="src/shop/application/discount.py",
            content=(
                '"""Discounts."""\n\n\n'
                "def apply_discount(total: int, percent: int) -> int:\n"
                '    """Return the total after applying a percentage discount."""\n'
                "    if percent <= 0:\n"
                "        return total\n"
                "    return total - (total * percent // 100)\n"
            ),
        ),
        ChangedFile(
            path="tests/test_discount.py",
            content=(
                '"""Discount tests."""\n\n'
                "from shop.application.discount import apply_discount\n\n\n"
                "def test_applies() -> None:\n"
                '    """A positive percent reduces the total."""\n'
                "    assert apply_discount(100, 10) == 90\n"
            ),
        ),
    ),
)

_CLEAN_2 = Fixture(
    "clean_migration_with_downgrade",
    None,
    _pr(
        ChangedFile(
            path="alembic/versions/0100_add_index.py",
            content=(
                '"""Add an index."""\n\n'
                "from alembic import op\n\n\n"
                "def upgrade() -> None:\n"
                '    """Create the index."""\n'
                '    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])\n\n\n'
                "def downgrade() -> None:\n"
                '    """Drop the index."""\n'
                '    op.drop_index("ix_orders_customer_id", table_name="orders")\n'
            ),
        ),
    ),
)


FIXTURES: tuple[Fixture, ...] = (
    _R1,
    _R2,
    _R3,
    _R4,
    _R5,
    _R6,
    _R7,
    _R8,
    _R9,
    _R10,
    _R11,
    _CLEAN_1,
    _CLEAN_2,
)

RULE_FIXTURES: tuple[Fixture, ...] = tuple(f for f in FIXTURES if f.expected_rule is not None)
CLEAN_FIXTURES: tuple[Fixture, ...] = tuple(f for f in FIXTURES if f.expected_rule is None)


__all__ = ["CLEAN_FIXTURES", "FIXTURES", "RULE_FIXTURES", "Fixture"]
