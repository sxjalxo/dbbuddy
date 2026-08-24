"""Regression tests for single-record lookups ("What is Alice's age?").

2026-07-08 QA pass. Record-level lookups grounded by a name literal were broken
in several ways, all engine-level:

1. `semantic_enhancer.enhance_query` rebuilt the query from lowercased tokens,
   destroying the capitalization the intent builder needs to spot a name
   ("age of Carol" → "age of carol" → no WHERE). It must only APPEND mappings.
2. `extract_columns_from_context` tokenized on whitespace, so "age?" and
   "Alice's" never matched column "age" — punctuation defeated the lookup.
3. `extract_value_filters` skipped a name at position 0 ("Alice age",
   "Alice details" produced no filter).
4. A lookup grounded ONLY by a name literal ("Alice details") died at the
   "No tables detected" guard because value filters were extracted too late.
5. Aggregation over a column the query names ("average age") fell through the
   measure-name heuristic to AVG(id) — a wrong answer.
"""

import pytest

from dbbuddy_core.intent_builder import (
    build_query_intent,
    extract_aggregation,
    extract_columns_from_context,
    extract_value_filters,
)
from dbbuddy_core.semantic_enhancer import enhance_query

SCHEMA = {"customers": ["id", "name", "email", "age", "city", "status"]}
COLUMN_TYPES = {
    "customers": {
        "id": "int", "name": "varchar", "email": "varchar",
        "age": "int", "city": "varchar", "status": "varchar",
    }
}


# ── 1. enhance_query preserves the original query (append-only) ───────────────

def test_enhance_query_preserves_case_and_only_appends():
    memory = {"mappings": {"amount": {"payments.amount": 5}},
              "column_usage": {}, "table_usage": {}}
    # No mapping hit → returned verbatim, case intact.
    assert enhance_query("age of Carol", memory, 2) == "age of Carol"
    # Mapping hit → original text untouched, mapping tokens appended.
    assert enhance_query("total amount", memory, 2) == "total amount payments.amount payments"


# ── 2. Column detection survives punctuation / possessives ───────────────────

@pytest.mark.parametrize("query", ["What is Alice's age?", "age of Carol!", "Alice   age"])
def test_column_match_ignores_punctuation(query):
    retrieved = [{"table": "customers", "column": "age", "score": 0.0}]
    cols = extract_columns_from_context(retrieved, query)
    assert cols == [{"table": "customers", "column": "age", "alias": "age", "aggregation": None}]


# ── 3. Name literal at any position becomes a WHERE filter ───────────────────

@pytest.mark.parametrize("query, value", [
    ("What is Alice's age?", "Alice"),  # possessive + trailing '?'
    ("Alice age", "Alice"),             # leading name (position 0)
    ("age of Carol", "Carol"),          # trailing name after "of"
    ("email of Alice", "Alice"),
    ("Alice details", "Alice"),         # name-only lookup
])
def test_name_literal_becomes_filter(query, value):
    filters = extract_value_filters(query, SCHEMA, ["customers"])
    assert filters == [{"column": "customers.name", "operator": "=", "value": value}]


def test_question_words_are_not_name_values():
    # "What"/"how"/"who" must never be mistaken for a name literal.
    assert extract_value_filters("What color", SCHEMA, ["customers"]) == []
    assert extract_value_filters("how many", SCHEMA, ["customers"]) == []


# ── 4. A name-only lookup resolves the table (no "No tables detected") ───────

def test_name_only_lookup_resolves_table_and_filter():
    # "Alice details": no column/table word, only a name literal. The intent
    # builder must seed the table from the name filter instead of raising.
    intent = build_query_intent("Alice details", [], None, SCHEMA, column_types=COLUMN_TYPES)
    assert intent["tables"] == ["customers"]
    assert {"column": "customers.name", "operator": "=", "value": "Alice"} in intent["filters"]


def test_record_field_lookup_selects_field_and_filters_name():
    intent = build_query_intent("What is Alice's age?", [{"table": "customers", "column": "age", "score": 0.0}],
                                None, SCHEMA, column_types=COLUMN_TYPES)
    assert intent["tables"] == ["customers"]
    assert {"column": "customers.name", "operator": "=", "value": "Alice"} in intent["filters"]
    assert intent["aggregation"] is None  # a field read, not an aggregate


# ── 5. Aggregation targets the column the query names ────────────────────────

@pytest.mark.parametrize("query, func", [
    ("average age", "AVG"),
    ("total age of customers", "SUM"),
    ("max age", "MAX"),
    ("min age", "MIN"),
])
def test_aggregation_uses_named_column_not_id(query, func):
    agg = extract_aggregation(query, [], SCHEMA, ["customers"])
    assert agg == {
        "function": func,
        "column": {"table": "customers", "column": "age", "alias": "age", "aggregation": None},
    }
