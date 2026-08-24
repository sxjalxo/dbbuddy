"""PostgreSQL folds unquoted identifiers to lower case, so mixed case must be quoted.

A table created as `"CUSTOMER_LOG"` is reported by `information_schema` as
`CUSTOMER_LOG`, and the compiler emitted it verbatim and unquoted. PostgreSQL then
folded the reference:

    ERROR:  relation "customer_log" does not exist

Nothing in the engine was wrong about the *name* — it used exactly what
introspection returned. The mistake was assuming an unquoted identifier means what
it says on every engine. It does on MySQL (with default settings), SQLite and SQL
Server; it does not on PostgreSQL.

This is the same family as the reserved-word quoting already handled — an
identifier that will not survive the parser as written — with case as the reason
instead of the word. Found by the first run of the dogfood suites against a real
PostgreSQL target, on the `legacy` dataset, which exists precisely because real
schemas are not tidy.

Quoting stays targeted: an all-lowercase name is left alone on PostgreSQL too, so
ordinary output is unchanged.
"""

import pytest

from dbbuddy_core.dialects import registry
from dbbuddy_core.sql.compiler import _needs_quoting


def _dialect(engine):
    try:
        return registry.get_dialect(engine)
    except Exception as exc:                      # noqa: BLE001
        pytest.skip(f"{engine} dialect unavailable: {exc}")


@pytest.mark.parametrize("name", ["CUSTOMER_LOG", "Customer", "orderItem", "MixedCase"])
def test_mixed_case_is_quoted_on_postgres(name):
    assert _needs_quoting(name, _dialect("postgresql")) is True


@pytest.mark.parametrize("name", ["customers", "order_items", "paid_at"])
def test_lower_case_is_left_alone_on_postgres(name):
    assert _needs_quoting(name, _dialect("postgresql")) is False


@pytest.mark.parametrize("engine", ["mysql", "sqlserver"])
def test_case_alone_does_not_force_quoting_elsewhere(engine):
    """Only engines that fold case need this, and quoting has a cost in readability."""
    assert _needs_quoting("CUSTOMER_LOG", _dialect(engine)) is False


def test_reserved_words_are_still_quoted_everywhere():
    for engine in ("postgresql", "mysql", "sqlserver"):
        assert _needs_quoting("order", _dialect(engine)) is True


def test_non_plain_identifiers_are_still_quoted_everywhere():
    for engine in ("postgresql", "mysql", "sqlserver"):
        assert _needs_quoting("line-item", _dialect(engine)) is True
        assert _needs_quoting("total amount", _dialect(engine)) is True


def test_no_dialect_keeps_the_previous_behaviour():
    """Callers without a dialect must not start quoting on case alone."""
    assert _needs_quoting("CUSTOMER_LOG", None) is False
    assert _needs_quoting("order", None) is True


def test_compiled_sql_quotes_a_mixed_case_table_on_postgres():
    from dbbuddy_core.execution import compile_sql

    plan = {
        "base_table": "CUSTOMER_LOG",
        "select": [{"table": "CUSTOMER_LOG", "column": "customer_id"}],
    }
    sql = compile_sql(plan, engine="postgresql")
    assert '"CUSTOMER_LOG"' in sql, sql
