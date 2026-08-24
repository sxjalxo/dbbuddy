"""A learned mapping must not cost the query its grain.

The semantic enhancer applies a learned mapping by appending the column
reference it stands for:

    "total amount by region"  ->  "total amount by region payments.amount payments"

The mapping is correct. Applying it is what breaks the query. The appended text
is indistinguishable from the user naming that column, so retrieval returns it,
it lands in ``intent["select"]``, and the branch that binds the *dimension* is
skipped because the select list already looks populated. The plan then has an
aggregate and no grain:

    SELECT SUM(payments.amount) FROM regions JOIN customers JOIN orders JOIN payments

One global total, presented as the answer to a "by region" question — and, before
the confidence work, at high confidence. An instance that had learned something
answered *worse* than a cold one, which is the opposite of the point.

The discriminator is that **the measure is never the grain**. When the question
asked to group and the only thing surviving into the select list is the
aggregation's own column, no dimension has been found yet, and the dimension
branch must still run.
"""

import pytest

from dbbuddy_core.intent_builder import strip_measure_only_select


AGG = {"function": "SUM", "column": {"table": "payments", "column": "amount"}}


def _col(table, column):
    return {"table": table, "column": column, "alias": column, "aggregation": None}


def test_measure_only_select_is_cleared_when_a_grain_was_asked_for():
    kept = [_col("payments", "amount")]
    assert strip_measure_only_select(kept, AGG, requested_grouping=True) == []


def test_a_real_dimension_survives():
    kept = [_col("regions", "region_name")]
    assert strip_measure_only_select(kept, AGG, requested_grouping=True) == kept


def test_a_dimension_alongside_the_measure_survives():
    """Only the measure is dropped; the grain it was hiding stays."""
    kept = [_col("regions", "region_name"), _col("payments", "amount")]
    assert strip_measure_only_select(kept, AGG, requested_grouping=True) == [
        _col("regions", "region_name")
    ]


def test_untouched_when_no_grain_was_asked_for():
    """"total amount" must not have its select cleared and a grain invented."""
    kept = [_col("payments", "amount")]
    assert strip_measure_only_select(kept, AGG, requested_grouping=False) == kept


@pytest.mark.parametrize("aggregation", [None, {}, {"function": "SUM"}])
def test_no_aggregation_means_nothing_to_strip(aggregation):
    kept = [_col("payments", "amount")]
    assert strip_measure_only_select(kept, aggregation, requested_grouping=True) == kept


def test_empty_select_is_returned_unchanged():
    assert strip_measure_only_select([], AGG, requested_grouping=True) == []
