"""Type-aware comparison framework (dbbuddy_core.type_handlers).

Comparisons are driven by each column's declared type, not keyword lists:
numeric columns get ordering + BETWEEN, date columns get ISO date ranges, text
columns keep equality. Invalid comparisons (name > 5) fail closed. New types are
added by registering a handler — the planner never changes.
"""

from datetime import date

import pytest

import dbbuddy_core.type_handlers as type_handlers
from dbbuddy_core.sql.compiler import compile_parameterized_sql
from dbbuddy_core.type_handlers import (
    InvalidComparisonError,
    TypeHandler,
    classify_sql_type,
    extract_comparisons,
    get_type_handler,
    register_type_handler,
)


@pytest.fixture
def pinned_today(monkeypatch):
    """Pin 'today' to 2026-07-04 (a Saturday) so calendar tests are stable."""
    monkeypatch.setattr(type_handlers, "_today", lambda: date(2026, 7, 4))
    return date(2026, 7, 4)

SCHEMA = {
    "users": ["id", "name", "age", "city", "created_at", "balance"],
    "orders": ["id", "user_id", "amount", "status", "ordered_at", "quantity"],
}
TYPES = {
    "users": {"id": "int", "name": "varchar(200)", "age": "int", "city": "varchar(100)",
              "created_at": "datetime", "balance": "decimal(10,2)"},
    "orders": {"id": "int", "user_id": "int", "amount": "decimal(10,2)",
               "status": "varchar(20)", "ordered_at": "timestamp", "quantity": "int"},
}


# ── Type classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("int", "numeric"), ("INT UNSIGNED", "numeric"), ("decimal(10,2)", "numeric"),
    ("bigint", "numeric"), ("double precision", "numeric"),
    ("date", "date"), ("datetime", "date"), ("timestamp without time zone", "date"),
    ("boolean", "boolean"), ("tinyint(1)", "numeric"),
    ("varchar(255)", "string"), ("text", "string"), (None, "string"), ("", "string"),
])
def test_classify_sql_type(raw, expected):
    assert classify_sql_type(raw) == expected


# ── Numeric comparisons ───────────────────────────────────────────────────────

@pytest.mark.parametrize("query, expected", [
    ("users with age > 30", {"column": "users.age", "operator": ">", "value": 30}),
    ("age >= 21", {"column": "users.age", "operator": ">=", "value": 21}),
    ("orders where amount over 100", {"column": "orders.amount", "operator": ">", "value": 100}),
    ("amount under 50", {"column": "orders.amount", "operator": "<", "value": 50}),
    ("balance at least 1000", {"column": "users.balance", "operator": ">=", "value": 1000}),
    ("quantity between 10 and 20", {"column": "orders.quantity", "operator": "BETWEEN", "value": [10, 20]}),
])
def test_numeric_comparisons(query, expected):
    assert expected in extract_comparisons(query, SCHEMA, TYPES)


def test_numeric_decimal_value_kept_as_float():
    got = extract_comparisons("amount > 99.95", SCHEMA, TYPES)
    assert got == [{"column": "orders.amount", "operator": ">", "value": 99.95}]


# ── Date comparisons (explicit ISO only) ──────────────────────────────────────

@pytest.mark.parametrize("query, expected", [
    ("users created after 2024-01-01", {"column": "users.created_at", "operator": ">", "value": "2024-01-01"}),
    ("orders ordered before 2025-06-30", {"column": "orders.ordered_at", "operator": "<", "value": "2025-06-30"}),
    ("created since 2024", {"column": "users.created_at", "operator": ">=", "value": "2024-01-01"}),
    ("ordered_at between 2024-01-01 and 2024-12-31",
     {"column": "orders.ordered_at", "operator": "BETWEEN", "value": ["2024-01-01", "2024-12-31"]}),
])
def test_date_comparisons(query, expected):
    assert expected in extract_comparisons(query, SCHEMA, TYPES)


def test_column_alias_drops_temporal_suffix():
    # "created" resolves to "created_at" (suffix dropped), derived from the name.
    got = extract_comparisons("show users created after 2023-01-01", SCHEMA, TYPES)
    assert got == [{"column": "users.created_at", "operator": ">", "value": "2023-01-01"}]


def test_non_iso_date_is_not_guessed():
    # Fuzzy dates are out of scope for now — no filter rather than a wrong one.
    assert extract_comparisons("users created after January 2024", SCHEMA, TYPES) == []


# ── Phase A: deterministic calendar expressions ───────────────────────────────

@pytest.mark.parametrize("expr, start, end", [
    ("today", "2026-07-04", "2026-07-05"),
    ("yesterday", "2026-07-03", "2026-07-04"),
    ("tomorrow", "2026-07-05", "2026-07-06"),
    ("this week", "2026-06-29", "2026-07-06"),   # ISO week starts Monday
    ("this month", "2026-07-01", "2026-08-01"),
    ("this year", "2026-01-01", "2027-01-01"),
])
def test_calendar_expressions_resolve_to_half_open_range(pinned_today, expr, start, end):
    got = extract_comparisons(f"orders {expr}", SCHEMA, TYPES)
    assert got == [
        {"column": "orders.ordered_at", "operator": ">=", "value": start},
        {"column": "orders.ordered_at", "operator": "<", "value": end},
    ]


def test_calendar_column_anchored(pinned_today):
    # Naming the column ties the calendar range to it directly.
    got = extract_comparisons("users created today", SCHEMA, TYPES)
    assert got == [
        {"column": "users.created_at", "operator": ">=", "value": "2026-07-04"},
        {"column": "users.created_at", "operator": "<", "value": "2026-07-05"},
    ]


def test_bare_calendar_is_ambiguous_with_two_date_columns(pinned_today):
    schema = {"log": ["id", "start_at", "end_at"]}
    types = {"log": {"id": "int", "start_at": "datetime", "end_at": "datetime"}}
    # Two date columns, none named → don't guess which one.
    assert extract_comparisons("log this month", schema, types) == []
    # Naming the column disambiguates.
    assert extract_comparisons("log start this month", schema, types) == [
        {"column": "log.start_at", "operator": ">=", "value": "2026-07-01"},
        {"column": "log.start_at", "operator": "<", "value": "2026-08-01"},
    ]


def test_calendar_needs_types(pinned_today):
    # Without types nothing is classified as a date → no calendar filter.
    assert extract_comparisons("orders this month", {"orders": ["id", "ordered_at"]}) == []


def test_bare_calendar_scoped_to_focus_table(pinned_today):
    # "revenue last month" names no table; the measure resolved to payments, so the
    # temporal phrase must attach to payments' date column — not be dropped as
    # ambiguous across the whole schema (regression: it returned no filter).
    schema = {
        "payments": ["id", "amount", "payment_date"],
        "orders": ["id", "order_date", "total"],
    }
    types = {
        "payments": {"id": "int", "amount": "numeric", "payment_date": "date"},
        "orders": {"id": "int", "order_date": "date", "total": "numeric"},
    }
    # Whole schema → two date columns, unscoped → still ambiguous, no guess.
    assert extract_comparisons("total revenue last month", schema, types) == []
    # Scoped to the measure's table → attaches to payments.payment_date.
    assert extract_comparisons(
        "total revenue last month", schema, types, focus_tables=["payments"]
    ) == [
        {"column": "payments.payment_date", "operator": ">=", "value": "2026-06-01"},
        {"column": "payments.payment_date", "operator": "<", "value": "2026-07-01"},
    ]


def test_bare_calendar_ignores_numeric_column_named_time(pinned_today):
    # A REAL column literally named ``time`` (a duration in seconds) is numeric,
    # not temporal — the name-convention branch must not promote it, or a bare
    # phrase ("last month") applies an ISO-date range to a float and silently
    # returns the wrong rows. Regression: it produced races.time >= '2026-06-01'.
    schema = {"races": ["id", "name", "time", "distance"]}
    types = {"races": {"id": "INTEGER", "name": "TEXT", "time": "REAL", "distance": "REAL"}}
    assert extract_comparisons(
        "average time last month", schema, types, focus_tables=["races"]
    ) == []


def test_bare_calendar_recognizes_text_dated_column_by_name(pinned_today):
    # SQLite (and any loosely-typed source) stores dates as TEXT, so a type-only
    # check would miss them. With types present, the naming convention (_date/_at)
    # still identifies the temporal column.
    schema = {"orders": ["id", "order_date", "freight"]}
    types = {"orders": {"id": "int", "order_date": "TEXT", "freight": "REAL"}}
    assert extract_comparisons(
        "total freight this year", schema, types, focus_tables=["orders"]
    ) == [
        {"column": "orders.order_date", "operator": ">=", "value": "2026-01-01"},
        {"column": "orders.order_date", "operator": "<", "value": "2027-01-01"},
    ]


def test_calendar_compiles_to_range(pinned_today):
    filters = extract_comparisons("orders this year", SCHEMA, TYPES)
    plan = {"base_table": "orders", "select": [{"table": "orders", "column": "*"}], "where": filters}
    sql, params = compile_parameterized_sql(plan)
    assert "WHERE orders.ordered_at >= %s AND orders.ordered_at < %s" in sql
    assert params == ["2026-01-01", "2027-01-01"]


# ── Phase B: deterministic relative expressions ───────────────────────────────
# pinned_today = 2026-07-04 (Saturday, Q3).

@pytest.mark.parametrize("expr, start, end", [
    ("last week", "2026-06-22", "2026-06-29"),     # prior ISO week (Mon–Sun)
    ("last month", "2026-06-01", "2026-07-01"),
    ("last quarter", "2026-04-01", "2026-07-01"),  # Q2, since today is Q3
    ("last year", "2025-01-01", "2026-01-01"),
    ("this quarter", "2026-07-01", "2026-10-01"),  # Q3
])
def test_relative_named_periods(pinned_today, expr, start, end):
    got = extract_comparisons(f"orders {expr}", SCHEMA, TYPES)
    assert got == [
        {"column": "orders.ordered_at", "operator": ">=", "value": start},
        {"column": "orders.ordered_at", "operator": "<", "value": end},
    ]


@pytest.mark.parametrize("expr, start, end", [
    ("past 30 days", "2026-06-04", "2026-07-05"),   # last 30 days incl. today
    ("last 7 days", "2026-06-27", "2026-07-05"),
    ("last 2 weeks", "2026-06-20", "2026-07-05"),
    ("next 7 days", "2026-07-04", "2026-07-11"),     # today forward
])
def test_relative_rolling_windows(pinned_today, expr, start, end):
    got = extract_comparisons(f"orders {expr}", SCHEMA, TYPES)
    assert got == [
        {"column": "orders.ordered_at", "operator": ">=", "value": start},
        {"column": "orders.ordered_at", "operator": "<", "value": end},
    ]


def test_relative_still_needs_types_and_is_unambiguous(pinned_today):
    # No types → not a date column → no relative filter.
    assert extract_comparisons("orders last month", {"orders": ["id", "ordered_at"]}) == []
    # Fuzzy phrasing is still not handled (Phase C).
    assert extract_comparisons("orders in Q1", SCHEMA, TYPES) == []


# ── String columns keep equality; ordering is invalid ─────────────────────────

def test_string_equality_unchanged():
    assert extract_comparisons("city Berlin", SCHEMA, TYPES) == \
        [{"column": "users.city", "operator": "=", "value": "Berlin"}]


def test_currency_symbol_and_thousands_separator():
    assert extract_comparisons("orders with amount over $1,000", SCHEMA, TYPES) == \
        [{"column": "orders.amount", "operator": ">", "value": 1000}]


def test_engineering_suffix_alias_with_collision_avoidance():
    schema = {"orders": ["id", "amount", "amount_usd", "weight_kg"]}
    types = {"orders": {"id": "int", "amount": "decimal", "amount_usd": "decimal", "weight_kg": "decimal"}}
    # "weight" resolves to weight_kg (no bare "weight" column).
    assert {"column": "orders.weight_kg", "operator": ">", "value": 100} in \
        extract_comparisons("weight over 100", schema, types)
    # "amount over 50" must hit the real `amount`, not double-match `amount_usd`
    # (its "amount" alias is dropped because an `amount` column exists).
    assert extract_comparisons("amount over 50", schema, types) == \
        [{"column": "orders.amount", "operator": ">", "value": 50}]


@pytest.mark.parametrize("query, col, value", [
    ("paid orders", "orders.is_paid", True),
    ("unpaid orders", "orders.is_paid", False),
    ("orders where is_paid = true", "orders.is_paid", True),
    ("orders that are not paid", "orders.is_paid", False),
])
def test_boolean_handler(query, col, value):
    schema = {"orders": ["id", "is_paid"]}
    types = {"orders": {"id": "int", "is_paid": "boolean"}}
    assert extract_comparisons(query, schema, types) == [{"column": col, "operator": "=", "value": value}]


def test_marker_word_after_column_is_not_a_value():
    # Regression (surfaced by the ERPNext schema): "customer named X" must NOT
    # capture the marker word "named" as the customer value.
    schema = {"invoices": ["id", "customer", "status"]}
    types = {"invoices": {"id": "int", "customer": "varchar", "status": "varchar"}}
    assert extract_comparisons("customer named Acme", schema, types) == []


def test_ordering_on_text_column_fails_closed():
    with pytest.raises(InvalidComparisonError):
        extract_comparisons("users where name > 5", SCHEMA, TYPES)


def test_no_types_degrades_to_equality_without_raising():
    # Without column types every column is text: equality only, and "age > 30"
    # produces nothing (no guess) rather than raising.
    assert extract_comparisons("users where age > 30", SCHEMA) == []
    assert extract_comparisons("city Berlin", SCHEMA) == \
        [{"column": "users.city", "operator": "=", "value": "Berlin"}]


# ── Compiles through the existing Predicate AST ───────────────────────────────

def test_typed_filters_compile_to_parameterized_sql():
    filters = extract_comparisons("orders where amount between 10 and 20", SCHEMA, TYPES)
    plan = {"base_table": "orders", "select": [{"table": "orders", "column": "*"}], "where": filters}
    sql, params = compile_parameterized_sql(plan)
    assert "WHERE orders.amount BETWEEN %s AND %s" in sql
    assert params == [10, 20]


# ── Extensibility: a new type only needs a handler ────────────────────────────

def test_register_new_type_handler(monkeypatch):
    class BooleanTypeHandler(TypeHandler):
        category = "boolean"

        def extract(self, query, table, column, col_lower, name_re, excluded, strict):
            import re
            m = re.search(rf"\b{name_re}\b\s+(?:is\s+)?(true|false)\b", query, re.IGNORECASE)
            if m:
                return [{"column": f"{table}.{column}", "operator": "=",
                         "value": m.group(1).lower() == "true"}]
            return []

    original = get_type_handler("boolean")
    register_type_handler(BooleanTypeHandler())
    try:
        schema = {"users": ["id", "is_active"]}
        types = {"users": {"id": "int", "is_active": "boolean"}}
        got = extract_comparisons("users where is_active is true", schema, types)
        assert got == [{"column": "users.is_active", "operator": "=", "value": True}]
    finally:
        register_type_handler(original)  # restore the default
