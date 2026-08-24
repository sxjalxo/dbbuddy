"""PostgreSQL-specific dialect unit tests (psycopg2 is mocked throughout)."""

from unittest.mock import MagicMock, patch
import pytest

# Guard: skip entire module if psycopg2 is not installed.
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

# Imported after the guard on purpose: the dialect module imports psycopg2 at
# module scope, so a top-of-file import would raise instead of skipping cleanly
# on a machine without the driver.
from dbbuddy_core.dialects.postgres import PostgresDialect  # noqa: E402
from dbbuddy_core.dialects.base import DialectConnection  # noqa: E402


@pytest.fixture
def dialect():
    return PostgresDialect()


@pytest.fixture
def mock_conn(dialect):
    raw = MagicMock()
    raw.closed = 0  # psycopg2: 0 means open
    return DialectConnection(raw, dialect)


# ── Metadata ──────────────────────────────────────────────────────────────────

def test_engine_name(dialect):
    assert dialect.engine_name == "postgresql"


def test_identifier_quote_char_is_double_quote(dialect):
    assert dialect.capabilities.identifier_quote_char == '"'


def test_supports_returning(dialect):
    assert dialect.capabilities.supports_returning is True


def test_supports_upsert(dialect):
    assert dialect.capabilities.supports_upsert is True


def test_supports_arrays(dialect):
    assert dialect.capabilities.supports_arrays is True


# ── SQL fragments ─────────────────────────────────────────────────────────────

def test_current_date(dialect):
    assert dialect.current_date() == "CURRENT_DATE"


def test_current_timestamp(dialect):
    assert dialect.current_timestamp() == "CURRENT_TIMESTAMP"


def test_random(dialect):
    assert dialect.random() == "RANDOM()"


def test_boolean_true(dialect):
    assert dialect.boolean_literal(True) == "TRUE"


def test_boolean_false(dialect):
    assert dialect.boolean_literal(False) == "FALSE"


def test_date_sub_interval(dialect):
    result = dialect.date_sub_interval("col", 1, "month")
    assert "col" in result and "1" in result and "month" in result


def test_date_add_interval(dialect):
    result = dialect.date_add_interval("col", 7, "day")
    assert "col" in result and "7" in result and "day" in result


def test_extract_month(dialect):
    assert dialect.extract_month("col") == "EXTRACT(MONTH FROM col)"


def test_extract_year(dialect):
    assert dialect.extract_year("col") == "EXTRACT(YEAR FROM col)"


def test_date_cast(dialect):
    assert dialect.date_cast("col") == "(col)::date"


def test_quote_identifier(dialect):
    assert dialect.quote_identifier("my table") == '"my table"'


def test_quote_identifier_escapes_double_quote(dialect):
    result = dialect.quote_identifier('tab"le')
    assert result == '"tab""le"'


def test_cast_integer(dialect):
    result = dialect.cast("price", "integer")
    assert "INTEGER" in result and "price" in result


def test_cast_text(dialect):
    result = dialect.cast("col", "text")
    assert "TEXT" in result


def test_regex_match(dialect):
    assert dialect.regex_match("email", "'@'") == "email ~ '@'"


def test_limit_no_offset(dialect):
    assert dialect.limit(20) == "LIMIT 20"


def test_limit_with_offset(dialect):
    assert dialect.limit(20, offset=10) == "LIMIT 20 OFFSET 10"


# ── Connection ────────────────────────────────────────────────────────────────

def test_connect_calls_psycopg2(dialect):
    with patch("dbbuddy_core.dialects.postgres.psycopg2.connect") as mock_connect:
        mock_raw = MagicMock()
        mock_connect.return_value = mock_raw

        conn = dialect.connect("localhost", "postgres", "pw", "testdb")
        mock_connect.assert_called_once_with(
            host="localhost", user="postgres", password="pw", dbname="testdb"
        )
        assert conn is mock_raw


def test_connect_passes_port_when_given(dialect):
    with patch("dbbuddy_core.dialects.postgres.psycopg2.connect") as mock_connect:
        dialect.connect("localhost", "postgres", "pw", "testdb", port=5433)
        mock_connect.assert_called_once_with(
            host="localhost", user="postgres", password="pw", dbname="testdb", port=5433
        )


def test_ping_open_connection(dialect):
    raw = MagicMock()
    raw.closed = 0
    dialect.ping(raw)  # Should not raise


def test_ping_closed_connection_raises(dialect):
    raw = MagicMock()
    raw.closed = 1
    with pytest.raises(RuntimeError, match="closed"):
        dialect.ping(raw)


def test_dict_cursor_uses_real_dict_cursor(dialect):
    raw = MagicMock()
    with patch("dbbuddy_core.dialects.postgres.psycopg2.extras.RealDictCursor") as mock_rdc:
        dialect.dict_cursor(raw)
        raw.cursor.assert_called_once_with(cursor_factory=mock_rdc)


# ── fetch_schema ──────────────────────────────────────────────────────────────

def test_fetch_schema_returns_dict(dialect):
    raw = MagicMock()
    cursor = MagicMock()
    raw.cursor.return_value = cursor
    cursor.fetchall.side_effect = [
        [("products",)],
        [("id",), ("name",), ("price",)],
    ]

    schema = dialect.fetch_schema(raw)
    assert schema == {"products": ["id", "name", "price"]}
