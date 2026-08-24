"""A grouping phrase must not cost the query its measure.

Observed on the demo schema:

    "total revenue"                  -> SUM(payments.amount)
    "total revenue by region"        -> no aggregation at all

Adding "by region" does not change what is being totalled, but it changes what
retrieval returns: the dimension's columns outrank the measure (``payments.amount``
fell from rank 2 to rank 8), table detection then keeps only ``regions``, and a
measure search confined to that table finds nothing. The question plainly asked
for a total and the plan carried none — and, because the intent never recorded an
aggregation, nothing downstream could tell that one had gone missing.

The engine already solves the mirror image of this in two places:
``find_numeric_column(widen=True)`` and ``extract_grouping_table``, which searches
the whole schema because "the dimension named after 'per' is frequently a table
the question never mentions otherwise". This is the same reasoning applied to the
measure instead of the dimension.
"""

import pytest

from dbbuddy_core.intent_builder import (
    aggregation_with_widening,
    has_aggregation_signal,
)

# Deliberately generic names. The engine must not know what any of these mean —
# the point is that the measure lives in a table the caller did not detect.
SCHEMA = {
    "regions": ["region_id", "region_name", "country"],
    "customers": ["customer_id", "customer_name", "region_id", "segment"],
    "payments": ["payment_id", "order_id", "paid_at", "amount", "method"],
}
COLUMN_TYPES = {
    "regions": {"region_id": "integer", "region_name": "varchar", "country": "varchar"},
    "customers": {"customer_id": "integer", "customer_name": "varchar",
                  "region_id": "integer", "segment": "varchar"},
    "payments": {"payment_id": "integer", "order_id": "integer",
                 "paid_at": "timestamp", "amount": "numeric", "method": "varchar"},
}
PRIMARY_KEYS = {"regions": ["region_id"], "customers": ["customer_id"], "payments": ["payment_id"]}


def _resolve(query, tables, select=None):
    return aggregation_with_widening(
        query, select or [], SCHEMA, tables, COLUMN_TYPES, PRIMARY_KEYS, []
    )


@pytest.mark.parametrize(
    "query",
    ["total amount", "total amount by region", "total amount by region last quarter"],
)
def test_measure_survives_a_grouping_phrase(query):
    """The measure table is missing from `tables` — the widening must find it."""
    agg = _resolve(query, tables=["regions"])
    assert agg, f"no aggregation resolved for {query!r}"
    assert agg["function"] == "SUM"
    assert agg["column"]["table"] == "payments"
    assert agg["column"]["column"] == "amount"


def test_detected_tables_are_still_preferred():
    """Widening is a fallback, not a free-for-all.

    When the detected tables *do* contain a usable measure, that is the answer —
    the widened search must not get a chance to prefer some other table's column.
    """
    agg = _resolve("total amount", tables=["payments"])
    assert agg["column"]["table"] == "payments"


def test_no_widening_without_an_aggregation_signal():
    """A question that asked for no total must not acquire one.

    Widening is gated on the query actually requesting an aggregation; otherwise
    every browse query would grow a SUM over whatever numeric column the schema
    happens to contain.
    """
    assert _resolve("show me the regions", tables=["regions"]) is None


def test_aggregation_signal_detection():
    assert has_aggregation_signal("total amount by region")
    assert has_aggregation_signal("how many orders")
    assert has_aggregation_signal("average order value")
    assert not has_aggregation_signal("show me the regions")
    assert not has_aggregation_signal("list customers in the north")
