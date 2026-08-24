"""SUM and AVG must target a numeric column, or not resolve at all.

The `legacy` dataset has a table called `Customer` with a column also called
`Customer`, holding text. Asked for "total customer lifetime value", the engine
matched the word and compiled:

    SELECT SUM("Customer"."Customer") FROM "Customer"

PostgreSQL:

    ERROR:  function sum(text) does not exist

SQLite would have accepted it and returned 0.0, because it coerces text to a
number and gives up quietly — another statement that only a real engine rejects.

The honest outcome is no aggregation. The question then carries an aggregation
signal the plan does not satisfy, the dropped-clause check sees that, and
confidence falls — the user is told the engine did not understand rather than
handed a zero that looks like an answer.

COUNT is exempt: counting rows of anything is well defined.
"""

import pytest

from dbbuddy_core.intent_builder import aggregation_is_type_safe

COLUMN_TYPES = {
    "Customer": {"Customer": "text", "balance": "numeric", "opened_on": "date"},
    "payments": {"amount": "numeric", "method": "varchar", "paid_at": "timestamp"},
}


def _agg(function, table, column):
    return {"function": function, "column": {"table": table, "column": column}}


@pytest.mark.parametrize("function", ["SUM", "AVG"])
def test_text_column_is_rejected(function):
    assert aggregation_is_type_safe(_agg(function, "Customer", "Customer"), COLUMN_TYPES) is False


@pytest.mark.parametrize("function", ["SUM", "AVG"])
def test_numeric_column_is_accepted(function):
    assert aggregation_is_type_safe(_agg(function, "payments", "amount"), COLUMN_TYPES) is True


@pytest.mark.parametrize("function", ["COUNT", "count"])
def test_count_is_exempt(function):
    """Counting rows of a text column is a perfectly good question."""
    assert aggregation_is_type_safe(_agg(function, "Customer", "Customer"), COLUMN_TYPES) is True


@pytest.mark.parametrize("function", ["MIN", "MAX"])
def test_min_and_max_are_exempt(function):
    """MIN/MAX are defined for text and dates — "earliest date", "last name".."""
    assert aggregation_is_type_safe(_agg(function, "Customer", "Customer"), COLUMN_TYPES) is True


def test_a_date_column_is_not_summable():
    assert aggregation_is_type_safe(_agg("SUM", "Customer", "opened_on"), COLUMN_TYPES) is False


def test_unknown_types_are_permitted():
    """With no type information the extractor's documented default is permissive.

    Refusing here would turn "we don't know" into "no", and silently disable
    aggregation for every caller that has no column types to offer.
    """
    assert aggregation_is_type_safe(_agg("SUM", "payments", "amount"), None) is True
    assert aggregation_is_type_safe(_agg("SUM", "unknown_table", "whatever"), COLUMN_TYPES) is True


def test_malformed_input_is_permitted():
    assert aggregation_is_type_safe(None, COLUMN_TYPES) is True
    assert aggregation_is_type_safe({}, COLUMN_TYPES) is True
    assert aggregation_is_type_safe({"function": "SUM"}, COLUMN_TYPES) is True
