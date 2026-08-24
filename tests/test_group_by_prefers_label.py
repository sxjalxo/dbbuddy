"""Grouping by a key answers at the right grain with the wrong label.

"total revenue by region" grouped by `regions.region_id`, so the result was eight
rows of integers. The grain is correct and the numbers are correct — it is the
answer to the question — but nobody can read it, and the near-identical "total
amount by region" grouped by `regions.region_name` instead. Same question, same
schema, two different answers depending on which word the user reached for.

The cause is upstream ordering: `grouping_column_refs` yields `region_id` before
`region_name` (both match the token "region"), and the first filter over the
select list accepts whichever arrives first. The dimension-binding branch further
down already knows better — it calls `_find_identifier_column`, which returns the
table's human label — but never runs, because the select list is already
non-empty.

So: when a chosen grain is a key and its own table has a label, use the label.
Fall back to the key when there is no label, which is the honest answer for a
table that has nothing else.
"""

from dbbuddy_core.intent_builder import prefer_label_over_key

SCHEMA = {
    "regions": ["region_id", "region_name", "country"],
    "payments": ["payment_id", "order_id", "paid_at", "amount"],
    "tags": ["tag_id"],
}
ROLES = {
    "regions": {"region_id": "identifier", "region_name": "person_name", "country": "dimension"},
    "payments": {"payment_id": "identifier", "amount": "measure"},
    "tags": {"tag_id": "identifier"},
}


def _col(table, column):
    return {"table": table, "column": column, "alias": column, "aggregation": None}


def test_key_is_swapped_for_its_tables_label():
    kept = [_col("regions", "region_id")]
    assert prefer_label_over_key(kept, SCHEMA, ROLES) == [_col("regions", "region_name")]


def test_a_label_is_left_alone():
    kept = [_col("regions", "region_name")]
    assert prefer_label_over_key(kept, SCHEMA, ROLES) == kept


def test_a_plain_dimension_is_left_alone():
    kept = [_col("regions", "country")]
    assert prefer_label_over_key(kept, SCHEMA, ROLES) == kept


def test_key_is_kept_when_the_table_has_no_label():
    """`tags` has nothing but its key — grouping by it is the honest answer."""
    kept = [_col("tags", "tag_id")]
    assert prefer_label_over_key(kept, SCHEMA, ROLES) == kept


def test_no_duplicate_when_the_label_is_already_present():
    kept = [_col("regions", "region_name"), _col("regions", "region_id")]
    assert prefer_label_over_key(kept, SCHEMA, ROLES) == [_col("regions", "region_name")]


def test_empty_and_missing_inputs_are_safe():
    assert prefer_label_over_key([], SCHEMA, ROLES) == []
    assert prefer_label_over_key([_col("regions", "region_id")], SCHEMA, None) == [
        _col("regions", "region_name")
    ], "roles are optional; the name convention still identifies the key"
