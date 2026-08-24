"""Guards for schemas that name columns after SQL keywords.

AirportDB names the two airport foreign keys on `flight` ``from`` and ``to``.
``from`` was already quoted; ``to`` was not, so every join through it compiled to
``ON flight.to = airport.airport_id`` and the database rejected the statement
with ``near "to": syntax error``. Half a reserved-word pair is worse than
neither — the schema is unqueryable in exactly the direction the user asks about
half the time.
"""

import pytest

from dbbuddy_core.sql.compiler import _needs_quoting, compile_sql


@pytest.mark.parametrize("name", [
    "from", "to", "on", "for", "with", "order", "group", "select", "where",
    "key", "value", "index", "user", "comment", "type", "status",
])
def test_reserved_words_are_quoted(name):
    assert _needs_quoting(name), f"{name!r} must be quoted to survive the parser"


@pytest.mark.parametrize("name", [
    "flight_id", "airport", "price", "capacity", "airlinename", "passenger_id",
    # Ordinary words that merely *contain* a keyword must stay unquoted: the
    # quoting rule is deliberately narrow so everyday SQL reads normally.
    "order_id", "from_date", "to_date", "user_name", "type_id",
])
def test_ordinary_identifiers_are_left_alone(name):
    assert not _needs_quoting(name)


def test_where_condition_on_a_reserved_word_table_is_quoted():
    """A filter on a reserved-word table must quote the WHERE qualifier too.

    Regression: SELECT/FROM/GROUP BY/HAVING routed through the quoting helpers, but
    WHERE conditions were handed to the operator framework with a raw column, so a
    table called ``order`` compiled to ``... FROM `order` WHERE order.status = %s``
    — a syntax error. Covers both the dotted-string form and the separate-``table``
    key form the planner emits.
    """
    plan = {
        "base_table": "order",
        "select": [{"table": "order", "column": "id", "alias": "id"}],
        "where": [
            {"column": "order.status", "operator": "=", "value": "paid"},
            {"table": "order", "column": "created_at", "operator": ">=",
             "value": "2026-06-01"},
        ],
    }
    for sql in (compile_sql(plan), compile_sql(plan, parameterize=True)[0]):
        # The reserved-word table qualifier is quoted in WHERE (status is also a
        # keyword, so it quotes too; created_at is ordinary and stays bare).
        assert ("`order`.`status`" in sql) or ('"order"."status"' in sql)
        assert ("`order`.created_at" in sql) or ('"order".created_at' in sql)
        # never the bare, parser-rejected qualifier
        assert "order.status" not in sql.replace("`order`", "").replace('"order"', "")
        assert "order.created_at" not in sql.replace("`order`", "").replace('"order"', "")


@pytest.mark.parametrize("order_by", [
    {"table": "order", "column": "group", "direction": "DESC"},   # dict + table key
    {"column": "order.group", "direction": "ASC"},                # dotted string
    [{"table": "order", "column": "group"}],                      # list form
])
def test_order_by_a_reserved_word_column_is_quoted(order_by):
    """ORDER BY on a reserved-word column must quote it and keep the qualifier.

    Regression: the simple-column ORDER BY paths emitted the raw column
    (``ORDER BY group DESC``) — a syntax error — and dropped the table qualifier,
    which is also ambiguous on a join. Only the aggregate ORDER BY path was quoted.
    """
    plan = {
        "base_table": "order",
        "select": [{"table": "order", "column": "id"}],
        "order_by": order_by,
    }
    for sql in (compile_sql(plan), compile_sql(plan, parameterize=True)[0]):
        assert ("ORDER BY `order`.`group`" in sql) or ('ORDER BY "order"."group"' in sql)
        assert "ORDER BY group" not in sql  # never the bare, parser-rejected form


def test_join_on_a_reserved_word_column_compiles():
    """The AirportDB shape: both join keys are keywords, one on each side."""
    plan = {
        "base_table": "flight",
        "joins": [{"left_table": "flight", "right_table": "airport",
                   "left_key": "to", "right_key": "airport_id"}],
        "select": [{"table": "flight", "column": "to", "alias": "to"}],
        "aggregation": {"function": "COUNT",
                        "column": {"table": "flight", "column": "flight_id"}},
        "group_by": [{"table": "flight", "column": "to"}],
    }
    sql = compile_sql(plan)
    assert '"to"' in sql or "`to`" in sql
    # …and never bare, which is the form the parser rejects.
    assert " flight.to " not in f" {sql} " and "flight.to =" not in sql
