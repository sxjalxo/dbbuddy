"""MySQL-specific dialect unit tests."""

from unittest.mock import MagicMock, patch
import pytest

from dbbuddy_core.dialects.mysql import MySQLDialect
from dbbuddy_core.dialects.base import DialectConnection


@pytest.fixture
def dialect():
    return MySQLDialect()


@pytest.fixture
def mock_conn(dialect):
    raw = MagicMock()
    return DialectConnection(raw, dialect)


# ── Metadata ──────────────────────────────────────────────────────────────────

def test_engine_name(dialect):
    assert dialect.engine_name == "mysql"


def test_capabilities_identifier_quote_is_backtick(dialect):
    assert dialect.capabilities.identifier_quote_char == "`"


def test_supports_upsert(dialect):
    assert dialect.capabilities.supports_upsert is True


def test_no_returning(dialect):
    assert dialect.capabilities.supports_returning is False


# ── SQL fragments ─────────────────────────────────────────────────────────────

def test_current_date(dialect):
    assert dialect.current_date() == "CURDATE()"


def test_current_timestamp(dialect):
    assert dialect.current_timestamp() == "NOW()"


def test_random(dialect):
    assert dialect.random() == "RAND()"


def test_boolean_true_is_1(dialect):
    assert dialect.boolean_literal(True) == "1"


def test_boolean_false_is_0(dialect):
    assert dialect.boolean_literal(False) == "0"


def test_date_sub_interval(dialect):
    assert dialect.date_sub_interval("col", 1, "month") == "DATE_SUB(col, INTERVAL 1 MONTH)"


def test_date_add_interval(dialect):
    assert dialect.date_add_interval("col", 7, "day") == "DATE_ADD(col, INTERVAL 7 DAY)"


def test_extract_month(dialect):
    assert dialect.extract_month("col") == "MONTH(col)"


def test_extract_year(dialect):
    assert dialect.extract_year("col") == "YEAR(col)"


def test_date_cast(dialect):
    assert dialect.date_cast("col") == "DATE(col)"


def test_quote_identifier(dialect):
    assert dialect.quote_identifier("my table") == "`my table`"


def test_quote_identifier_escapes_backtick(dialect):
    assert dialect.quote_identifier("tab`le") == "`tab``le`"


def test_cast_integer(dialect):
    result = dialect.cast("price", "integer")
    assert "CAST" in result
    assert "SIGNED" in result


def test_cast_text(dialect):
    result = dialect.cast("age", "text")
    assert "CHAR" in result


def test_regex_match(dialect):
    assert dialect.regex_match("email", "'@'") == "email REGEXP '@'"


def test_limit_no_offset(dialect):
    assert dialect.limit(20) == "LIMIT 20"


def test_limit_with_offset(dialect):
    assert dialect.limit(20, offset=10) == "LIMIT 20 OFFSET 10"


# ── Connection ────────────────────────────────────────────────────────────────

def test_connect_success(dialect):
    with patch("dbbuddy_core.dialects.mysql.mysql.connector.connect") as mock_connect:
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = True
        mock_connect.return_value = mock_raw

        conn = dialect.connect("localhost", "root", "pw", "db")
        assert conn is mock_raw


def test_connect_not_connected_raises(dialect):
    with patch("dbbuddy_core.dialects.mysql.mysql.connector.connect") as mock_connect:
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = False
        mock_connect.return_value = mock_raw

        with pytest.raises(RuntimeError, match="disconnected"):
            dialect.connect("localhost", "root", "pw", "db")


def test_ping_calls_raw_ping(dialect):
    raw = MagicMock()
    dialect.ping(raw)
    raw.ping.assert_called_once_with(reconnect=True, attempts=2, delay=1)


def test_dict_cursor(dialect):
    raw = MagicMock()
    dialect.dict_cursor(raw)
    raw.cursor.assert_called_once_with(dictionary=True)


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
