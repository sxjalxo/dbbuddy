"""Semantic column roles — deterministic prior (Phase A).

`dbbuddy_core.semantic_roles.classify_roles` assigns each column a role from its
name + declared SQL type (no AI, no data). The planner uses `person_name` roles
to ground a name literal on the *right* identifier column instead of guessing by
sentence position or list order — e.g. a table with `username`, `first_name`,
`last_name` binds "Alice" to `first_name`, not `username`.

See docs/SEMANTIC_ROLES.md.
"""

import pytest

from dbbuddy_core.semantic_roles import (
    DIMENSION, FREE_TEXT, IDENTIFIER, MEASURE, PERSON_NAME, TEMPORAL,
    ai_role_from_term, classify_column, classify_roles, identifier_columns_for,
    overlay_ai_roles,
)
from dbbuddy_core.intent_builder import _find_identifier_column, extract_value_filters

SCHEMA = {"members": ["id", "username", "first_name", "last_name", "age", "city", "joined_at", "bio"]}
TYPES = {"members": {
    "id": "int", "username": "varchar", "first_name": "varchar", "last_name": "varchar",
    "age": "int", "city": "varchar", "joined_at": "datetime", "bio": "text",
}}


# ── classify_column: name + type → role ──────────────────────────────────────

@pytest.mark.parametrize("col, sql_type, expected", [
    ("id", "int", IDENTIFIER),
    ("customer_id", "bigint", IDENTIFIER),      # FK, numeric but a key not a measure
    ("uuid", "char", IDENTIFIER),
    ("name", "varchar", PERSON_NAME),
    ("first_name", "varchar", PERSON_NAME),
    ("age", "int", MEASURE),
    ("amount", "decimal", MEASURE),
    ("created_at", "timestamp", TEMPORAL),
    ("order_date", "date", TEMPORAL),
    ("status", "varchar", DIMENSION),
    ("city", "varchar", DIMENSION),
    ("description", "text", FREE_TEXT),
])
def test_classify_column(col, sql_type, expected):
    assert classify_column(col, sql_type) == expected


def test_zip_and_phone_numbers_are_not_measures():
    # Numeric by type but a code/identifier by name — never SUM(zip).
    assert classify_column("zip_code", "int") != MEASURE
    assert classify_column("phone_number", "bigint") != MEASURE


def test_classify_roles_whole_schema():
    roles = classify_roles(SCHEMA, TYPES)["members"]
    assert roles["age"] == MEASURE
    assert roles["joined_at"] == TEMPORAL
    assert roles["first_name"] == PERSON_NAME
    assert roles["bio"] == FREE_TEXT


def test_roles_degrade_without_types():
    # No column_types: name heuristics alone still classify the obvious ones.
    roles = classify_roles(SCHEMA)["members"]
    assert roles["id"] == IDENTIFIER
    assert roles["first_name"] == PERSON_NAME
    assert roles["joined_at"] == TEMPORAL  # "_at" suffix, no type needed


# ── identifier ranking: real name before login handle ───────────────────────

def test_identifier_columns_rank_name_before_handle():
    roles = classify_roles(SCHEMA, TYPES)
    assert identifier_columns_for("members", SCHEMA, roles) == \
        ["first_name", "last_name", "username"]


def test_identifier_columns_empty_without_roles():
    # No roles → [] so the caller falls back to its own name heuristic.
    assert identifier_columns_for("members", SCHEMA, None) == []


# ── role-driven grounding in the intent builder ─────────────────────────────

def test_find_identifier_prefers_person_name_role():
    roles = classify_roles(SCHEMA, TYPES)
    # Role-driven picks the given name; the legacy heuristic would take "username"
    # (it is an exact _IDENTIFIER_COLUMNS entry).
    assert _find_identifier_column("members", SCHEMA, roles) == "first_name"
    assert _find_identifier_column("members", SCHEMA) == "username"


def test_value_filter_binds_to_role_identifier():
    roles = classify_roles(SCHEMA, TYPES)
    assert extract_value_filters("What is Alice's age?", SCHEMA, ["members"], roles) == \
        [{"column": "members.first_name", "operator": "=", "value": "Alice"}]


def test_value_filter_without_roles_still_grounds():
    # Backward-compatible: no roles → heuristic identifier column, still a WHERE.
    filters = extract_value_filters("What is Alice's age?", SCHEMA, ["members"])
    assert filters == [{"column": "members.username", "operator": "=", "value": "Alice"}]


# ── Phase B: AI classification overlays the deterministic prior ──────────────

@pytest.mark.parametrize("term, expected", [
    ("name", PERSON_NAME),
    ("identifier", IDENTIFIER),
    ("date", TEMPORAL),
    ("quantity", MEASURE),
    ("status", DIMENSION),
    ("description", FREE_TEXT),
    ("value", None),        # deliberately ambiguous — prior decides
    ("email", None),        # not a category word
    ("", None),
    (None, None),
])
def test_ai_role_from_term(term, expected):
    assert ai_role_from_term(term) == expected


def test_overlay_promotes_unrecognized_text_to_person_name():
    # "borrower" is a person but has no _name suffix and is text → prior=dimension.
    # The AI classified it "name"; the overlay upgrades the weak default.
    schema = {"loans": ["id", "borrower", "amount", "memo"]}
    types = {"loans": {"id": "int", "borrower": "varchar", "amount": "decimal", "memo": "text"}}
    prior = classify_roles(schema, types)
    assert prior["loans"]["borrower"] == DIMENSION  # prior can't tell
    semantic = {"loans": {
        "borrower": {"term": "name", "source": "ai"},
        "memo": {"term": "description", "source": "ai"},
    }}
    overlay_ai_roles(prior, semantic)
    assert prior["loans"]["borrower"] == PERSON_NAME  # AI filled the gap
    assert prior["loans"]["memo"] == FREE_TEXT


def test_overlay_never_overrides_a_confident_prior():
    # A structural signal (id, numeric measure, _name, date type) must survive even
    # if the AI disagrees — the prior is authoritative for those.
    schema = {"t": ["id", "amount", "first_name", "created_at"]}
    types = {"t": {"id": "int", "amount": "decimal", "first_name": "varchar", "created_at": "datetime"}}
    prior = classify_roles(schema, types)
    semantic = {"t": {  # deliberately wrong AI labels
        "id": {"term": "name", "source": "ai"},
        "amount": {"term": "description", "source": "ai"},
        "first_name": {"term": "status", "source": "ai"},
        "created_at": {"term": "name", "source": "ai"},
    }}
    overlay_ai_roles(prior, semantic)
    assert prior["t"] == {
        "id": IDENTIFIER, "amount": MEASURE, "first_name": PERSON_NAME, "created_at": TEMPORAL,
    }


def test_overlay_ignores_rule_sourced_and_missing_labels():
    schema = {"t": ["notes"]}
    types = {"t": {"notes": "text"}}
    prior = classify_roles(schema, types)  # notes → free_text
    # source=="rule" (no provider answered) must not overlay.
    overlay_ai_roles(prior, {"t": {"notes": {"term": "name", "source": "rule"}}})
    assert prior["t"]["notes"] == FREE_TEXT
    # No semantic at all → pure prior, no error.
    assert overlay_ai_roles(classify_roles(schema, types), {}) == {"t": {"notes": FREE_TEXT}}
