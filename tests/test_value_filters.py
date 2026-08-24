"""Value-filter extraction: a named literal ("Alice") must become a WHERE filter.

Regression for the bug where "Give me Alice's details" produced
``SELECT users.name FROM users`` with no WHERE and returned every row, because
``extract_filters`` only knew a few hardcoded literals. ``extract_value_filters``
grounds a detected name literal on a table's identifier column. It is
high-precision: analytical queries (aggregations, measures, time ranges) must
never produce a spurious value filter.
"""

import pytest

from dbbuddy_core.intent_builder import (
    extract_filters,
    extract_grouping_table,
    extract_value_filters,
    infer_table_from_filters,
)
from dbbuddy_core.sql.compiler import compile_parameterized_sql

SCHEMA = {
    "users": ["id", "name", "email", "country", "created_at"],
    "orders": ["id", "user_id", "total_amount", "status", "created_at"],
}

# A completely different domain — the engine must work with no code changes.
HEALTHCARE = {
    "patients": ["id", "full_name", "blood_type", "ward"],
    "appointments": ["id", "patient_id", "department", "status"],
}


@pytest.mark.parametrize("query", [
    "Give me Alice's details",
    "Give me user Alice's details",
    "Show user Alice",
    "Find user named Alice",
    "Get Alice",
])
def test_named_literal_becomes_name_filter(query):
    filters = extract_value_filters(query, SCHEMA, ["users"])
    assert filters == [{"column": "users.name", "operator": "=", "value": "Alice"}]


@pytest.mark.parametrize("query", [
    "Show all users",
    "Show total revenue last month",
    "Top 10 customers by lifetime value",
    "Daily active users for the past 30 days",
    "Conversion rate by traffic source this quarter",
    "Show Monthly Revenue by Region",  # Title-case analytics must not be mistaken for a value
])
def test_analytical_queries_produce_no_value_filter(query):
    assert extract_value_filters(query, SCHEMA, ["users"]) == []


def test_value_filter_ignores_schema_and_stopwords():
    # "users" (table) and "details" (stopword) are never treated as values.
    assert extract_value_filters("Show all user details", SCHEMA, ["users"]) == []


def test_value_filter_grounds_when_no_table_detected_yet():
    # A bare "Get Bob" with no table pre-detected still resolves to a name column.
    filters = extract_value_filters("Get Bob", SCHEMA, [])
    assert filters == [{"column": "users.name", "operator": "=", "value": "Bob"}]


def test_value_filter_compiles_to_parameterized_where():
    filters = extract_value_filters("Give me Alice's details", SCHEMA, ["users"])
    # Same shape the planner copies verbatim into execution_plan["where"].
    plan = {
        "base_table": "users",
        "select": [{"table": "users", "column": "name"}],
        "where": filters,
    }
    sql, params = compile_parameterized_sql(plan)
    assert "WHERE users.name = %s" in sql
    assert params == ["Alice"]  # value is bound, never interpolated


# ── Schema-driven "<column> = <value>" filters (no hardcoded values) ──────────

@pytest.mark.parametrize("query, expected", [
    ("users with country = India", {"column": "users.country", "operator": "=", "value": "India"}),
    ("show payments where payment_method upi",
     {"column": "payments.payment_method", "operator": "=", "value": "upi"}),
    ("subscriptions where status is active",
     {"column": "subscriptions.status", "operator": "=", "value": "active"}),
])
def test_explicit_column_value_filters_are_schema_driven(query, expected):
    schema = {
        "users": ["id", "name", "country", "status"],
        "payments": ["id", "user_id", "amount", "payment_method"],
        "subscriptions": ["id", "user_id", "plan", "status"],
    }
    assert expected in extract_filters(query, schema)


def test_column_filters_work_on_an_unseen_domain():
    # No commerce vocabulary — proves the extractor keys off the live schema only.
    assert {"column": "appointments.department", "operator": "=", "value": "Cardiology"} in \
        extract_filters("appointments where department Cardiology", HEALTHCARE)
    assert {"column": "patients.ward", "operator": "=", "value": "ICU"} in \
        extract_filters("show patients in ward ICU", HEALTHCARE)


def test_no_hardcoded_value_guessing():
    # Legacy hardcoding turned bare "active"/"upi"/"india" into filters even when
    # the column was never named. That demo-fitting behavior is gone.
    schema = {"subscriptions": ["id", "status"], "payments": ["id", "payment_method"]}
    assert extract_filters("show active subscriptions", schema) == []
    assert extract_filters("payments via upi", schema) == []


def test_infer_table_from_filters_is_schema_driven():
    # Table read straight off a qualified column…
    assert infer_table_from_filters([{"column": "payments.amount"}], SCHEMA) == "payments"
    # …or looked up in the schema for a bare column (no hardcoded column→table map).
    assert infer_table_from_filters([{"column": "country"}], SCHEMA) == "users"
    assert infer_table_from_filters([{"column": "blood_type"}], HEALTHCARE) == "patients"


def test_grouping_table_matches_any_schema_entity():
    assert extract_grouping_table("revenue per user", ["users", "payments"]) == "users"
    # Same "per <entity>" logic on a domain the code has never seen.
    assert extract_grouping_table("appointments per patient", ["patients", "appointments"]) == "patients"
