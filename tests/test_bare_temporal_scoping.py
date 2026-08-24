"""A bare temporal phrase must not be lost because the question named a dimension.

"total revenue by region last quarter" names `regions`, which has no date column.
Scope selection was `named_tables or focus or schema`, so naming *any* table shut
out the tables the planner had actually resolved — the measure's table, where the
date column lives. The phrase resolved to nothing and the filter was dropped
silently: the answer covered all time and said so nowhere.

It only worked by accident when the semantic enhancer happened to append the
measure's table to the query text ("… payments.amount payments"), which put
`payments` into `named_tables`. So the same question was time-filtered on an
instance that had learned a mapping and unfiltered on one that had not.

The fix keeps the narrowing the comment describes — a named table wins — but
treats each scope as a candidate tier rather than a veto: try the named tables,
then the planner's resolved tables, then the whole schema, and take the first tier
that yields exactly one date column. Ambiguity still means "do not guess" at every
tier.
"""

from datetime import date

from dbbuddy_core.type_handlers import extract_comparisons

SCHEMA = {
    "regions": ["region_id", "region_name", "country"],
    "customers": ["customer_id", "customer_name", "region_id", "segment"],
    "payments": ["payment_id", "order_id", "paid_at", "amount", "method"],
    "orders": ["order_id", "customer_id", "order_date", "status"],
}
COLUMN_TYPES = {
    "regions": {"region_id": "integer", "region_name": "varchar", "country": "varchar"},
    "customers": {"customer_id": "integer", "customer_name": "varchar",
                  "region_id": "integer", "segment": "varchar"},
    "payments": {"payment_id": "integer", "order_id": "integer",
                 "paid_at": "timestamp", "amount": "numeric", "method": "varchar"},
    "orders": {"order_id": "integer", "customer_id": "integer",
               "order_date": "date", "status": "varchar"},
}


def _filters(query, focus_tables):
    return extract_comparisons(query, SCHEMA, COLUMN_TYPES, focus_tables=focus_tables)


def _columns(filters):
    return sorted({f["column"] for f in filters})


def test_named_dimension_without_a_date_does_not_veto_the_measure_table():
    """The regression: `regions` is named, `payments` holds the date."""
    filters = _filters("total revenue by region last quarter", ["regions", "payments"])
    assert _columns(filters) == ["payments.paid_at"], filters


def test_table_order_does_not_change_the_answer():
    a = _filters("total revenue by region last quarter", ["regions", "payments"])
    b = _filters("total revenue by region last quarter", ["payments", "regions"])
    assert a == b


def test_a_named_table_with_a_date_still_wins():
    """Narrowing is the point: naming `orders` must pick `orders.order_date`.

    Both `orders` and `payments` are in focus and both carry a date, so without
    the named tier this would be ambiguous and produce nothing.
    """
    filters = _filters("how many orders last quarter", ["orders", "payments"])
    assert _columns(filters) == ["orders.order_date"], filters


def test_still_refuses_to_guess_when_the_tier_is_ambiguous():
    """Two candidate date columns and nothing to choose between them."""
    filters = _filters("total last quarter", ["orders", "payments"])
    assert filters == []


def test_bounds_are_a_half_open_range():
    filters = _filters("total revenue by region last quarter", ["regions", "payments"])
    ops = sorted(f["operator"] for f in filters)
    assert ops == ["<", ">="]
    lo = next(f["value"] for f in filters if f["operator"] == ">=")
    hi = next(f["value"] for f in filters if f["operator"] == "<")
    assert date.fromisoformat(lo) < date.fromisoformat(hi)
