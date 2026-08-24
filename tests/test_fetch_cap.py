"""The result set a query may pull into this process is bounded at the cursor.

`execute_query` used to call `cursor.fetchall()` and leave truncation to
`execution.MAX_ROWS`, which runs *after* every row is already a Python dict in
memory. That is survivable on a test database and fatal on a real one: against
AirportDB's 54.3M-row `booking` table, `SELECT * FROM booking` materialised the
whole table and took the machine down. The display limit cannot fix this — only
not reading the rows can.
"""

import pytest

from dbbuddy_core import query as query_module
from dbbuddy_core.query import FETCH_CAP, execute_query


class _HugeCursor:
    """A cursor over more rows than anyone should read, counting what is read."""

    def __init__(self, total: int):
        self.total = total
        self.served = 0
        self.fetchall_calls = 0

    def execute(self, sql, params=None):
        return None

    def fetchmany(self, size):
        take = min(size, self.total - self.served)
        self.served += take
        return [{"id": self.served - take + i} for i in range(take)]

    def fetchall(self):
        self.fetchall_calls += 1
        return [{"id": i} for i in range(self.total)]


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, dictionary=False):
        return self._cursor


def test_read_stops_at_the_cap_and_never_calls_fetchall():
    cursor = _HugeCursor(total=10_000_000)
    rows = execute_query(_Conn(cursor), "SELECT * FROM booking")
    assert len(rows) == FETCH_CAP
    assert cursor.served == FETCH_CAP, "read beyond the cap"
    assert cursor.fetchall_calls == 0, "fetchall materialises the whole result set"


def test_small_result_is_returned_whole():
    cursor = _HugeCursor(total=42)
    rows = execute_query(_Conn(cursor), "SELECT * FROM airline")
    assert len(rows) == 42


def test_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(query_module, "FETCH_CAP", 25)
    cursor = _HugeCursor(total=1000)
    rows = execute_query(_Conn(cursor), "SELECT * FROM booking")
    assert len(rows) == 25


@pytest.mark.parametrize("statement", ["SELECT 1; DELETE FROM users", "select 1;drop table t"])
def test_stacked_statements_still_rejected(statement):
    with pytest.raises(ValueError):
        execute_query(_Conn(_HugeCursor(1)), statement)


# ── The bound also travels with the statement ────────────────────────────────
#
# Reading in batches keeps this process alive; it does not stop the database
# from producing 54M rows or the network from carrying them. The planner sends a
# limit so the scan ends early.

def test_a_row_returning_plan_gets_a_limit():
    from dbbuddy_core.execution import MAX_ROWS
    from dbbuddy_core.query_planner import _bound_unlimited_read

    plan = {"base_table": "booking", "select": [{"table": "booking", "column": "*"}]}
    _bound_unlimited_read(plan)
    assert plan["limit"] == MAX_ROWS + 1, "one past the display limit proves 'more exist'"
    assert plan["limit_is_guardrail"] is True


def test_an_explicit_limit_is_never_overridden():
    from dbbuddy_core.query_planner import _bound_unlimited_read

    plan = {"base_table": "booking", "limit": 5}
    _bound_unlimited_read(plan)
    assert plan["limit"] == 5


@pytest.mark.parametrize("plan", [
    {"base_table": "booking", "aggregation": {"function": "COUNT",
                                              "column": {"column": "booking_id"}}},
    {"base_table": "booking", "group_by": [{"table": "booking", "column": "seat"}]},
])
def test_aggregates_and_groupings_are_left_alone(plan):
    """An aggregate returns one row anyway, and limiting a grouped query would
    drop *groups* — a wrong answer rather than a truncated one."""
    from dbbuddy_core.query_planner import _bound_unlimited_read

    _bound_unlimited_read(plan)
    assert plan.get("limit") is None


def test_the_limit_reaches_the_sql():
    from dbbuddy_core.execution import MAX_ROWS
    from dbbuddy_core.query_planner import _bound_unlimited_read
    from dbbuddy_core.sql.compiler import compile_sql

    plan = {"base_table": "booking",
            "select": [{"table": "booking", "column": "booking_id"}],
            "joins": []}
    _bound_unlimited_read(plan)
    assert f"LIMIT {MAX_ROWS + 1}" in compile_sql(plan)
