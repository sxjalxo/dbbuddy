"""ORDER BY compilation: the aggregate must survive as an expression.

These exist because the entire aggregate-aware ORDER BY path was unreachable.
``compile_sql`` wraps a dict ``order_by`` into a single-element list early (so
later code can iterate safely), and the assembly block then tested
``isinstance(order_by, dict)`` — never true after the wrap. Every query fell
through to the list branch, which quotes whatever string it is handed.

The visible result was ``ORDER BY "SUM(payments.amount)" DESC``: an aggregate
expression quoted as though it were a column name. PostgreSQL rejects it —

    ERROR:  column "SUM(payments.amount)" does not exist

— so "top N X by Y", the most common analytical question there is, failed on the
documented production engine.

**SQLite hides this.** It accepts a double-quoted unknown identifier as a string
constant, so the same SQL returns rows, unsorted, with no error. Every test and
all eight dogfood datasets run on SQLite, which is why a green suite coexisted
with a broken clause for so long. ``test_sqlite_would_not_have_caught_this``
pins that fact so nobody re-derives it the hard way.

Assertions here are on the emitted SQL, deliberately: executing on SQLite is
exactly the check that cannot see the bug.
"""

import sqlite3

import pytest

from dbbuddy_core.execution import compile_sql

# A grouped, joined, ranked plan — the shape behind "top 5 customers by total
# payments". Built per test so a mutation in one cannot leak into another.
def _ranked_plan(order_by):
    return {
        "base_table": "customers",
        "select": [
            {"table": "customers", "column": "customer_name"},
            {"table": "payments", "column": "amount", "aggregation": "SUM"},
        ],
        "joins": [
            {"table": "orders", "on": "customers.customer_id = orders.customer_id", "type": "INNER"},
            {"table": "payments", "on": "orders.order_id = payments.order_id", "type": "INNER"},
        ],
        "group_by": [
            {"table": "customers", "column": "customer_name"},
            {"table": "customers", "column": "customer_id"},
        ],
        "aggregation": {"function": "SUM", "column": {"table": "payments", "column": "amount"}},
        "order_by": order_by,
        "limit": 5,
    }


def _order_by_clause(sql: str) -> str:
    assert "ORDER BY" in sql, f"no ORDER BY emitted: {sql!r}"
    return sql[sql.index("ORDER BY"):]


# Every shape a plan has been observed to carry. They must all compile to the
# same aggregate expression — the caller's spelling of the spec is not a
# semantic difference.
@pytest.mark.parametrize(
    "order_by",
    [
        pytest.param({"column": "SUM(payments.amount)", "direction": "DESC"}, id="dict-preformatted"),
        pytest.param(
            {"column": "SUM(payments.amount)", "table": "payments", "direction": "DESC"},
            id="dict-preformatted-with-table",
        ),
        pytest.param(
            {"column": "amount", "table": "payments", "aggregation": "SUM", "direction": "DESC"},
            id="dict-structured",
        ),
        pytest.param([{"column": "SUM(payments.amount)", "direction": "DESC"}], id="list-of-dict"),
        pytest.param({"column": "amount", "table": "payments", "direction": "DESC"}, id="plain-column-reuses-plan-aggregate"),
    ],
)
def test_aggregate_order_by_is_an_expression_not_an_identifier(order_by):
    clause = _order_by_clause(compile_sql(_ranked_plan(order_by)))

    assert "SUM(payments.amount)" in clause.replace("`", "").replace('"', ""), clause
    # The aggregate must not be quoted as a single identifier. Quoting the whole
    # expression is the defect; quoting the parts inside it would be fine.
    assert "`SUM(" not in clause and '"SUM(' not in clause, (
        f"aggregate quoted as an identifier: {clause}"
    )
    assert clause.rstrip().split()[-1] != "DESC" or "DESC" in clause


def test_direction_is_preserved():
    clause = _order_by_clause(
        compile_sql(_ranked_plan({"column": "SUM(payments.amount)", "direction": "DESC"}))
    )
    assert "DESC" in clause


def test_reserved_word_column_is_still_quoted():
    """The fix must not cost the identifier quoting that a real schema needs."""
    plan = {
        "base_table": "orders",
        "select": [{"table": "orders", "column": "order_id"}],
        "order_by": {"table": "orders", "column": "group", "direction": "ASC"},
    }
    clause = _order_by_clause(compile_sql(plan))
    assert "`group`" in clause or '"group"' in clause, clause


def test_plain_column_order_by_is_qualified():
    plan = {
        "base_table": "orders",
        "select": [{"table": "orders", "column": "order_id"}],
        "order_by": {"table": "orders", "column": "order_date", "direction": "DESC"},
    }
    clause = _order_by_clause(compile_sql(plan))
    assert "orders.order_date" in clause, clause


def test_sqlite_would_not_have_caught_this():
    """Pin *why* the suite stayed green: SQLite reads the broken form as a string.

    A quoted unknown identifier is a string constant in SQLite, so ordering by it
    is ordering by a constant — no error, no sorting, plausible rows. Asserting
    on executed results against SQLite is therefore not a valid check for this
    class of bug, and a regression test that did so would pass while production
    was broken.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, 5), (2, 9), (3, 1)])

    rows = conn.execute(
        'SELECT a, SUM(b) AS sum_b FROM t GROUP BY a ORDER BY "SUM(t.b)" DESC'
    ).fetchall()

    # No exception, and no sorting: the rows come back in scan order, not by b.
    assert [r[0] for r in rows] != [2, 1, 3], "SQLite unexpectedly sorted by the aggregate"
