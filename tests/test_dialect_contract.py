"""Contract tests — every Dialect implementation must satisfy these.

Tests are parametrized over all dialect classes so that adding a new engine
means implementing the contract and passing the shared suite. No real database
connections are required; raw connections are fully mocked.
"""

import pytest
from unittest.mock import MagicMock

from dbbuddy_core.dialects.base import DialectConnection
from dbbuddy_core.dialects.capabilities import DialectCapabilities
from dbbuddy_core.dialects.schema_meta import DatabaseSchema, TableMeta, ColumnMeta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mysql_dialect():
    from dbbuddy_core.dialects.mysql import MySQLDialect
    return MySQLDialect()


def _make_postgres_dialect():
    pytest.importorskip("psycopg2", reason="psycopg2 not installed — skipping postgres contract tests")
    from dbbuddy_core.dialects.postgres import PostgresDialect
    return PostgresDialect()


def _make_sqlserver_dialect():
    pytest.importorskip("pymssql", reason="pymssql not installed — skipping SQL Server contract tests")
    from dbbuddy_core.dialects.sqlserver import SQLServerDialect
    return SQLServerDialect()


DIALECT_FACTORIES = [
    pytest.param(_make_mysql_dialect, id="mysql"),
    pytest.param(_make_postgres_dialect, id="postgres"),
    pytest.param(_make_sqlserver_dialect, id="sqlserver"),
]


# ---------------------------------------------------------------------------
# Contract: class-level metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_engine_name_is_nonempty_string(factory):
    d = factory()
    assert isinstance(d.engine_name, str)
    assert d.engine_name not in ("", "unknown")


# ---------------------------------------------------------------------------
# Contract: capabilities
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_capabilities_is_DialectCapabilities(factory):
    d = factory()
    assert isinstance(d.capabilities, DialectCapabilities)


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_capabilities_all_booleans(factory):
    cap = factory().capabilities
    bool_fields = [
        "supports_json", "supports_arrays", "supports_cte",
        "supports_window_functions", "supports_lateral",
        "supports_full_text", "supports_returning", "supports_upsert",
        "supports_regex",
    ]
    for field in bool_fields:
        assert isinstance(getattr(cap, field), bool), f"{field} must be bool"


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_identifier_quote_char_is_single_char(factory):
    cap = factory().capabilities
    assert isinstance(cap.identifier_quote_char, str)
    assert len(cap.identifier_quote_char) == 1


# ---------------------------------------------------------------------------
# Contract: SQL scalar methods return non-empty strings
# ---------------------------------------------------------------------------

SCALAR_METHODS = [
    ("current_date", [], "SELECT {}"),
    ("current_timestamp", [], "SELECT {}"),
    ("random", [], "SELECT {}"),
]

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
@pytest.mark.parametrize("method,args,_tmpl", SCALAR_METHODS)
def test_scalar_method_returns_nonempty_string(factory, method, args, _tmpl):
    result = getattr(factory(), method)(*args)
    assert isinstance(result, str) and result.strip()


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_boolean_literal_true(factory):
    v = factory().boolean_literal(True)
    assert v.strip().upper() in ("TRUE", "1", "YES")


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_boolean_literal_false(factory):
    v = factory().boolean_literal(False)
    assert v.strip().upper() in ("FALSE", "0", "NO")


# ---------------------------------------------------------------------------
# Contract: date/time fragment methods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_date_sub_interval(factory):
    result = factory().date_sub_interval("created_at", 1, "month")
    assert isinstance(result, str) and "created_at" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_date_add_interval(factory):
    result = factory().date_add_interval("created_at", 7, "day")
    assert isinstance(result, str) and "created_at" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_extract_month(factory):
    result = factory().extract_month("order_date")
    assert isinstance(result, str) and "order_date" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_extract_year(factory):
    result = factory().extract_year("order_date")
    assert isinstance(result, str) and "order_date" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_date_cast(factory):
    result = factory().date_cast("created_at")
    assert isinstance(result, str) and "created_at" in result


# ---------------------------------------------------------------------------
# Contract: identifier quoting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_quote_identifier_wraps_name(factory):
    d = factory()
    q = d.capabilities.identifier_quote_char
    result = d.quote_identifier("my_table")
    assert result.startswith(q) and result.endswith(q)
    assert "my_table" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_quote_identifier_escapes_embedded_quote(factory):
    d = factory()
    q = d.capabilities.identifier_quote_char
    result = d.quote_identifier(f"tab{q}le")
    assert result.count(q) >= 3  # open + escaped inner + close


# ---------------------------------------------------------------------------
# Contract: limit/offset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_limit_no_offset(factory):
    result = factory().limit(10)
    assert "10" in result


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_limit_with_offset(factory):
    result = factory().limit(10, offset=5)
    assert "10" in result and "5" in result


# ---------------------------------------------------------------------------
# Contract: regex_match (only tested when capability is True)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_regex_match_when_supported(factory):
    d = factory()
    if not d.capabilities.supports_regex:
        pytest.skip("dialect does not support regex")
    result = d.regex_match("email", "'@example\\.com$'")
    assert isinstance(result, str) and "email" in result


# ---------------------------------------------------------------------------
# Contract: concat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_concat_two_args(factory):
    result = factory().concat("first_name", "' '", "last_name")
    assert isinstance(result, str)
    assert "first_name" in result and "last_name" in result


# ---------------------------------------------------------------------------
# Contract: cast
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_cast_returns_string(factory):
    result = factory().cast("age", "integer")
    assert isinstance(result, str) and "age" in result


# ---------------------------------------------------------------------------
# Contract: DialectConnection wraps correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_dialect_connection_cursor_plain(factory):
    d = factory()
    raw = MagicMock()
    dc = DialectConnection(raw, d)
    dc.cursor()
    raw.cursor.assert_called_once()


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_dialect_connection_close(factory):
    d = factory()
    raw = MagicMock()
    dc = DialectConnection(raw, d)
    dc.close()
    raw.close.assert_called_once()


@pytest.mark.parametrize("factory", DIALECT_FACTORIES)
def test_dialect_connection_commit(factory):
    d = factory()
    raw = MagicMock()
    dc = DialectConnection(raw, d)
    dc.commit()
    raw.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Contract: fetch_schema returns {table: [col, ...]} — mocked DB
# ---------------------------------------------------------------------------

def test_mysql_fetch_schema_shape():
    from dbbuddy_core.dialects.mysql import MySQLDialect

    d = MySQLDialect()
    raw = MagicMock()
    cursor = MagicMock()
    raw.cursor.return_value = cursor
    cursor.fetchall.side_effect = [
        [("users",), ("orders",)],  # SHOW TABLES
        [("id",), ("name",)],       # DESCRIBE users
        [("id",), ("amount",)],     # DESCRIBE orders
    ]

    result = d.fetch_schema(raw)
    assert result == {"users": ["id", "name"], "orders": ["id", "amount"]}


def test_postgres_fetch_schema_shape():
    pytest.importorskip("psycopg2", reason="psycopg2 not installed")
    from dbbuddy_core.dialects.postgres import PostgresDialect

    d = PostgresDialect()
    raw = MagicMock()
    cursor = MagicMock()
    raw.cursor.return_value = cursor
    cursor.fetchall.side_effect = [
        [("users",), ("orders",)],  # information_schema.tables
        [("id",), ("name",)],       # information_schema.columns for users
        [("id",), ("amount",)],     # information_schema.columns for orders
    ]

    result = d.fetch_schema(raw)
    assert result == {"users": ["id", "name"], "orders": ["id", "amount"]}


# ---------------------------------------------------------------------------
# Contract: DatabaseSchema.to_simple() round-trips correctly
# ---------------------------------------------------------------------------

def test_database_schema_to_simple():
    schema = DatabaseSchema(
        tables={
            "users": TableMeta(
                name="users",
                columns=[
                    ColumnMeta(name="id", data_type="int", is_primary_key=True),
                    ColumnMeta(name="email", data_type="varchar"),
                ],
            )
        }
    )
    simple = schema.to_simple()
    assert simple == {"users": ["id", "email"]}
