"""Guards for star/snowflake schemas with role-playing dimensions.

TPC-DS shape: one fact reaching one dimension through several foreign keys
(``ws_sold_date_sk`` and ``ws_ship_date_sk`` both point at ``date_dim``), three
parallel channels whose measures share a name, surrogate ``_sk`` keys beside
business ``_id`` keys, and nullable foreign keys in the facts.

Each test corresponds to a wrong answer the dogfood loop produced: a question
about shipping answered with sale dates, a fact table joined to its siblings, a
row count that quietly dropped its NULL rows, and a measure bound to the shorter
of two columns that both matched.
"""

from dbbuddy_core.intent_builder import (
    _count_anchor_column,
    _match_measure_column,
    detect_tables_from_query,
    expand_query_tokens,
    grouping_column_refs,
    singular_forms,
)
from dbbuddy_core.query_planner import _apply_join_roles
from dbbuddy_core.relationship_graph import JoinKeys, build_relationship_graph

SCHEMA = {
    "web_sales": ["ws_sold_date_sk", "ws_ship_date_sk", "ws_item_sk",
                  "ws_bill_customer_sk", "ws_ship_customer_sk", "ws_order_number",
                  "ws_quantity", "ws_sales_price", "ws_ext_sales_price",
                  "ws_net_profit"],
    "store_sales": ["ss_sold_date_sk", "ss_item_sk", "ss_ticket_number",
                    "ss_quantity", "ss_sales_price", "ss_ext_sales_price",
                    "ss_net_profit"],
    "date_dim": ["d_date_sk", "d_date_id", "d_date", "d_year", "d_moy"],
    "customer": ["c_customer_sk", "c_customer_id", "c_first_name", "c_last_name"],
    "customer_address": ["ca_address_sk", "ca_city", "ca_state"],
}

FOREIGN_KEYS = {
    "web_sales": [
        ("ws_sold_date_sk", "date_dim", "d_date_sk"),
        ("ws_ship_date_sk", "date_dim", "d_date_sk"),
        ("ws_bill_customer_sk", "customer", "c_customer_sk"),
        ("ws_ship_customer_sk", "customer", "c_customer_sk"),
    ],
    "store_sales": [("ss_sold_date_sk", "date_dim", "d_date_sk")],
}

PRIMARY_KEYS = {
    "web_sales": ["ws_item_sk", "ws_order_number"],
    "store_sales": ["ss_item_sk", "ss_ticket_number"],
    "date_dim": ["d_date_sk"],
    "customer": ["c_customer_sk"],
    "customer_address": ["ca_address_sk"],
}


def test_graph_keeps_every_foreign_key_between_a_pair():
    graph = build_relationship_graph(SCHEMA, FOREIGN_KEYS, PRIMARY_KEYS)
    edge = graph["web_sales"]["date_dim"]
    assert tuple(edge) == ("ws_sold_date_sk", "d_date_sk")   # first declared wins
    assert isinstance(edge, JoinKeys)
    assert ("ws_ship_date_sk", "d_date_sk") in edge.alternates
    # Still a plain tuple everywhere it is consumed.
    left, right = graph["web_sales"]["date_dim"]
    assert (left, right) == ("ws_sold_date_sk", "d_date_sk")


def test_join_role_follows_the_words_the_question_used():
    graph = build_relationship_graph(SCHEMA, FOREIGN_KEYS, PRIMARY_KEYS)
    joins = [{"left_table": "web_sales", "right_table": "date_dim",
              "left_key": "ws_sold_date_sk", "right_key": "d_date_sk"}]
    _apply_join_roles(joins, graph,
                      sorted(expand_query_tokens("total net profit by ship date")),
                      SCHEMA)
    assert joins[0]["left_key"] == "ws_ship_date_sk"

    joins = [{"left_table": "web_sales", "right_table": "customer",
              "left_key": "ws_bill_customer_sk", "right_key": "c_customer_sk"}]
    _apply_join_roles(joins, graph,
                      sorted(expand_query_tokens("how many web sales per ship customer")),
                      SCHEMA)
    assert joins[0]["left_key"] == "ws_ship_customer_sk"


def test_join_role_left_alone_when_the_question_names_none():
    graph = build_relationship_graph(SCHEMA, FOREIGN_KEYS, PRIMARY_KEYS)
    joins = [{"left_table": "web_sales", "right_table": "date_dim",
              "left_key": "ws_sold_date_sk", "right_key": "d_date_sk"}]
    _apply_join_roles(joins, graph,
                      sorted(expand_query_tokens("total net profit by date")), SCHEMA)
    assert joins[0]["left_key"] == "ws_sold_date_sk"


def test_grouping_column_prefers_the_role_the_question_named():
    refs = grouping_column_refs("total net profit by ship date", SCHEMA, ["web_sales"])
    assert refs[0] == ("web_sales", "ws_ship_date_sk")
    refs = grouping_column_refs("total net profit by sold date", SCHEMA, ["web_sales"])
    assert refs[0] == ("web_sales", "ws_sold_date_sk")


def test_count_anchors_on_the_declared_key_not_a_nullable_foreign_key():
    """A fact's leading columns are nullable foreign keys, and COUNT skips NULLs
    — anchoring there returned 117,632 of 120,000 rows."""
    assert _count_anchor_column("store_sales", SCHEMA, PRIMARY_KEYS) == "ss_item_sk"
    assert _count_anchor_column("customer", SCHEMA, PRIMARY_KEYS) == "c_customer_sk"
    # Without declared keys it still avoids inventing one.
    assert _count_anchor_column("store_sales", SCHEMA) in SCHEMA["store_sales"]


def test_measure_prefers_the_column_that_consumes_more_of_the_question():
    match = _match_measure_column(
        expand_query_tokens("total store sales ext sales price"), set(),
        SCHEMA, ["store_sales"])
    assert match["column"] == "ss_ext_sales_price"

    match = _match_measure_column(
        expand_query_tokens("total store sales sales price"), set(),
        SCHEMA, ["store_sales"])
    assert match["column"] == "ss_sales_price"


def test_measure_records_a_tie_in_another_table():
    schema = {"invoices": ["amount"], "payments": ["amount"]}
    ties: list = []
    match = _match_measure_column(expand_query_tokens("total amount"), set(),
                                  schema, ["invoices"], ties)
    assert match["table"] == "invoices"
    assert ties == ["payments.amount"]


def test_plural_multiword_table_names_resolve():
    assert "address" in singular_forms("addresses")
    assert "customer_address" in [t for t in detect_tables_from_query(
        "how many customer addresses", SCHEMA)]
    # The shorter table that shares the prefix must not win.
    assert detect_tables_from_query("how many customer addresses", SCHEMA) \
        == ["customer_address"]
