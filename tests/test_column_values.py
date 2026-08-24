"""Dimension value index (Phase C) — data-driven literal grounding.

Roles say ``city`` is a dimension; the value index says ``Pune`` *is a city*. A
literal that exactly matches a sampled dimension value binds to that column
("customers in Pune" → ``WHERE city = 'Pune'``) instead of the name column.
Sampling is bounded (cardinality / value-length / column-count caps) and
best-effort. See docs/SEMANTIC_ROLES.md.
"""

from contextlib import contextmanager

from dbbuddy_core import column_values
from dbbuddy_core.semantic_roles import classify_roles
from dbbuddy_core.column_values import build_value_index, resolve_value
from dbbuddy_core.intent_builder import extract_value_filters

SCHEMA = {"customers": ["id", "name", "age", "city", "status"]}
TYPES = {"customers": {"id": "int", "name": "varchar", "age": "int", "city": "varchar", "status": "varchar"}}


class _Dialect:
    def quote_identifier(self, n):
        return f"`{n}`"


def _fake_connect(table_col_rows):
    """A connect() context manager whose cursor serves rows by the (table, col)
    referenced in the executed SQL — mimics `SELECT DISTINCT col FROM table`."""
    @contextmanager
    def connect():
        class Cur:
            def cursor(self, dictionary=False):
                return self

            def execute(self, sql):
                self._rows = []
                for (t, c), rows in table_col_rows.items():
                    if f"`{c}`" in sql and f"`{t}`" in sql:
                        self._rows = rows
                        break

            def fetchmany(self, n):
                return self._rows[:n]

            def close(self):
                pass
        yield Cur()
    return connect


def _roles():
    return classify_roles(SCHEMA, TYPES)


# ── sampling ─────────────────────────────────────────────────────────────────

def test_build_indexes_dimension_values_only():
    connect = _fake_connect({
        ("customers", "city"): [{"v": "Pune"}, {"v": "Mumbai"}, {"v": "Pune"}],
        ("customers", "status"): [{"v": "active"}, {"v": "inactive"}],
        # name is person_name (not a dimension) → never queried/indexed.
    })
    idx = build_value_index(connect, SCHEMA, _roles(), _Dialect())
    assert idx["pune"] == [["customers", "city"]]
    assert idx["active"] == [["customers", "status"]]
    assert "alice" not in idx  # name column is not sampled


def test_high_cardinality_dimension_is_skipped():
    many = [{"v": f"val{i}"} for i in range(column_values.MAX_DISTINCT + 5)]
    connect = _fake_connect({("customers", "city"): many, ("customers", "status"): [{"v": "active"}]})
    idx = build_value_index(connect, SCHEMA, _roles(), _Dialect())
    assert not any(k.startswith("val") for k in idx)   # city dropped (too many)
    assert idx["active"] == [["customers", "status"]]   # status still indexed


def test_numeric_and_overlong_values_not_indexed():
    connect = _fake_connect({("customers", "city"): [
        {"v": "12345"}, {"v": "x" * (column_values.MAX_VALUE_LEN + 1)}, {"v": "Pune"},
    ], ("customers", "status"): []})
    idx = build_value_index(connect, SCHEMA, _roles(), _Dialect())
    assert list(idx) == ["pune"]


def test_build_without_roles_is_empty():
    connect = _fake_connect({("customers", "city"): [{"v": "Pune"}]})
    assert build_value_index(connect, SCHEMA, None, _Dialect()) == {}


def test_sampling_error_is_swallowed_per_column():
    @contextmanager
    def broken():
        raise RuntimeError("connection lost")
        yield  # pragma: no cover
    assert build_value_index(broken, SCHEMA, _roles(), _Dialect()) == {}


# ── resolution ───────────────────────────────────────────────────────────────

def test_resolve_value_case_insensitive_and_misses():
    idx = {"pune": [["customers", "city"]]}
    assert resolve_value("Pune", idx, ["customers"]) == ("customers", "city")
    assert resolve_value("pune", idx, None) == ("customers", "city")
    assert resolve_value("Alice", idx, None) is None
    assert resolve_value("Pune", None, None) is None


def test_resolve_prefers_in_play_table_on_collision():
    idx = {"open": [["tickets", "state"], ["loans", "status"]]}
    assert resolve_value("open", idx, ["loans"]) == ("loans", "status")
    assert resolve_value("open", idx, []) == ("tickets", "state")  # first otherwise


# ── grounding: dimension value beats name grounding ─────────────────────────

def test_value_filter_binds_literal_to_dimension_column():
    idx = {"pune": [["customers", "city"]]}
    filters = extract_value_filters("customers in Pune", SCHEMA, ["customers"], _roles(), idx)
    assert filters == [{"column": "customers.city", "operator": "=", "value": "Pune"}]


def test_name_still_grounds_when_not_a_dimension_value():
    idx = {"pune": [["customers", "city"]]}
    filters = extract_value_filters("What is Alice's age?", SCHEMA, ["customers"], _roles(), idx)
    assert filters == [{"column": "customers.name", "operator": "=", "value": "Alice"}]


def test_without_value_index_literal_falls_back_to_name():
    # Phase B behavior preserved when no value index is present.
    filters = extract_value_filters("customers in Pune", SCHEMA, ["customers"], _roles())
    assert filters == [{"column": "customers.name", "operator": "=", "value": "Pune"}]
