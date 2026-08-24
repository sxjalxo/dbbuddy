"""Guards for schemas that prefix and concatenate their column names.

TPC-H (and every warehouse or mainframe-derived schema shaped like it) writes
``l_extendedprice`` on ``lineitem`` and ``ps_supplycost`` on ``partsupp``: a
one-to-three character table prefix, then the words of the name run together with
no separator at all. Splitting such a name cannot recover the boundary, so the
matching has to work from the query side — and the ``key`` suffix has to read as
a key, since nothing in the schema is named ``id``.

Each test here corresponds to a wrong answer the dogfood loop produced: a measure
bound to an unrelated column, a breakdown that collapsed to one row, a SUM over a
surrogate key, and a ranking that grouped by its own measure.
"""

from dbbuddy_core.intent_builder import (
    _count_anchor_column,
    _is_plausible_measure,
    _match_measure_column,
    column_match_tokens,
    expand_query_tokens,
    extract_aggregation,
    extract_grouping_table,
    grouping_column_refs,
    ranking_dimension_phrase,
    segment_token,
    uniform_column_prefix,
)

TPCH = {
    "lineitem": ["l_orderkey", "l_partkey", "l_suppkey", "l_linenumber",
                 "l_quantity", "l_extendedprice", "l_discount", "l_tax",
                 "l_returnflag", "l_linestatus", "l_shipdate", "l_shipmode"],
    "orders": ["o_orderkey", "o_custkey", "o_orderstatus", "o_totalprice",
               "o_orderdate", "o_orderpriority", "o_clerk"],
    "customer": ["c_custkey", "c_name", "c_address", "c_nationkey", "c_phone",
                 "c_acctbal", "c_mktsegment"],
    "nation": ["n_nationkey", "n_name", "n_regionkey", "n_comment"],
    "part": ["p_partkey", "p_name", "p_brand", "p_size", "p_retailprice"],
}

# A schema that repeats a real word at the head of every column. It must NOT be
# treated as prefixed: "order" is a word the user says.
WORDY = {"orders": ["order_id", "order_date", "order_total", "order_status"]}


def test_uniform_prefix_detected_only_when_short_and_universal():
    assert uniform_column_prefix(TPCH["lineitem"]) == "l"
    assert uniform_column_prefix(TPCH["customer"]) == "c"
    assert uniform_column_prefix(WORDY["orders"]) is None       # "order" is a word
    assert uniform_column_prefix(["id", "name", "email"]) is None


def test_column_tokens_drop_the_prefix_but_keep_real_words():
    assert column_match_tokens("l_extendedprice", "l") == ["extendedprice"]
    assert column_match_tokens("order_total", None) == ["order", "total"]


def test_query_tokens_reach_a_concatenated_column():
    tokens = expand_query_tokens("total extended price by ship mode")
    assert "extendedprice" in tokens
    assert "shipmode" in tokens
    # Plurals a user types that a column never carries.
    assert "orderpriority" in expand_query_tokens("top 10 order priorities")


def test_segmentation_uses_only_the_words_the_user_typed():
    vocab = expand_query_tokens("total order price")
    assert segment_token("totalprice", vocab) == ["total", "price"]
    # An abbreviation the query cannot name must fail rather than be guessed.
    assert segment_token("acctbal", expand_query_tokens("account balance")) is None
    assert segment_token("mktsegment", expand_query_tokens("market segment")) is None


def test_measure_binds_to_the_concatenated_column_not_a_key():
    match = _match_measure_column(expand_query_tokens("total extended price"),
                                  set(), TPCH, ["lineitem"])
    assert (match["table"], match["column"]) == ("lineitem", "l_extendedprice")

    match = _match_measure_column(expand_query_tokens("total order price"),
                                  set(), TPCH, ["orders"])
    assert (match["table"], match["column"]) == ("orders", "o_totalprice")


def test_key_columns_are_never_measures():
    for column in ("l_orderkey", "c_custkey", "n_regionkey"):
        prefix = column[0] if column[1] == "_" else None
        assert not _is_plausible_measure(column, prefix)
    assert _is_plausible_measure("l_extendedprice", "l")
    assert _is_plausible_measure("c_acctbal", "c")


def test_count_anchors_on_a_key_when_no_column_is_named_id():
    assert _count_anchor_column("supplier", {"supplier": ["s_suppkey", "s_name"]}) \
        == "s_suppkey"
    assert _count_anchor_column("users", {"users": ["id", "email"]}) == "id"


def test_unnamed_measure_does_not_wander_to_another_table():
    """"total account balance of customers" has no matchable measure in
    ``customer``; the answer must stay in the table the question named rather
    than summing an unrelated fact column."""
    agg = extract_aggregation("total account balance of customers", [], TPCH,
                              ["customer"])
    assert agg is None or agg["column"]["table"] == "customer"


def test_grouping_dimension_is_found_in_the_schema_not_the_context():
    refs = grouping_column_refs("total quantity per return flag", TPCH, ["lineitem"])
    assert ("lineitem", "l_returnflag") in refs


def test_ranking_reverses_the_role_of_by():
    assert ranking_dimension_phrase("top 3 ship modes by total extended price") \
        == "ship modes"
    assert ranking_dimension_phrase("total extended price by ship mode") is None

    # The dimension is what is ranked, so the measure search must not exclude
    # the measure and must not group on it.
    refs = grouping_column_refs("top 3 ship modes by total extended price",
                                TPCH, ["lineitem"])
    assert refs and refs[0] == ("lineitem", "l_shipmode")
    assert extract_grouping_table("top 5 nations by number of customers",
                                  ["customer"], TPCH) == "nation"


def test_per_entity_finds_a_table_the_question_never_named():
    assert extract_grouping_table("how many suppliers per region",
                                  ["supplier"],
                                  {**TPCH, "region": ["r_regionkey", "r_name"],
                                   "supplier": ["s_suppkey", "s_name"]}) == "region"
