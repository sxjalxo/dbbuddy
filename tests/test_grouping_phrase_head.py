"""A multi-word grouping phrase names one dimension, not every word in it.

"total amount by product category" grouped by `product_name`, `category` *and*
`product_id`, returning 60 rows — one per product — for a question that asked for
five categories. Both `product_name` and `category` match a token in "product
category", and the filter that picks the grain keeps every match.

English puts the head noun last: "product category" is a kind of category, not a
kind of product. So when several candidate dimensions survive and one of them
matches the *head* of the grouping phrase, that is the dimension.

Deliberately conservative. If narrowing would leave nothing, the original set is
kept — a phrase whose head names no column at all ("by total payment amount",
where the head belongs to the measure) must not strip the real grain.
"""

import pytest

from dbbuddy_core.intent_builder import narrow_to_phrase_head


def _col(table, column):
    return {"table": table, "column": column, "alias": column, "aggregation": None}


PRODUCT_NAME = _col("products", "product_name")
CATEGORY = _col("products", "category")


def test_head_noun_wins_over_the_modifier():
    kept = [PRODUCT_NAME, CATEGORY]
    assert narrow_to_phrase_head(kept, "total amount by product category") == [CATEGORY]


def test_single_word_phrase_is_unchanged():
    kept = [_col("regions", "region_name")]
    assert narrow_to_phrase_head(kept, "total amount by region") == kept


def test_nothing_is_dropped_when_the_head_matches_no_candidate():
    """"by total payment amount" — the head belongs to the measure, not the grain."""
    kept = [_col("customers", "customer_name")]
    assert narrow_to_phrase_head(kept, "top 5 customers by total payment amount") == kept


def test_all_candidates_kept_when_several_match_the_head():
    """Ambiguity is not resolved here; it is left for the ambiguity signal."""
    kept = [_col("products", "category"), _col("orders", "category")]
    assert narrow_to_phrase_head(kept, "total by category") == kept


@pytest.mark.parametrize("query", ["total revenue", "show me the products", ""])
def test_no_grouping_phrase_means_no_narrowing(query):
    kept = [PRODUCT_NAME, CATEGORY]
    assert narrow_to_phrase_head(kept, query) == kept


def test_per_and_for_each_are_grouping_phrases_too():
    kept = [PRODUCT_NAME, CATEGORY]
    assert narrow_to_phrase_head(kept, "revenue per product category") == [CATEGORY]
    assert narrow_to_phrase_head(kept, "revenue for each product category") == [CATEGORY]


def test_empty_input_is_safe():
    assert narrow_to_phrase_head([], "total amount by product category") == []


def test_an_appended_learned_mapping_does_not_break_the_phrase():
    """The enhancer appends "payments.amount payments" to the query text.

    A qualified reference is never part of what the user asked to group by, so it
    has to terminate the phrase. Without that, the phrase failed to match at all
    on any instance that had learned a mapping — the narrowing stopped working
    precisely where the enhancer had already made the grain harder to find.
    """
    kept = [PRODUCT_NAME, CATEGORY]
    enhanced = "total amount by product category payments.amount payments"
    assert narrow_to_phrase_head(kept, enhanced) == [CATEGORY]
