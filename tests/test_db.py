import unittest
from unittest.mock import patch, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from dbbuddy_core.db import connect_db
from dbbuddy_core.dialects.base import DialectConnection


# All tests mock at the MySQLDialect level since db.py now delegates there.
_MYSQL_CONNECT = "dbbuddy_core.dialects.mysql.mysql.connector.connect"


class TestConnectDb(unittest.TestCase):

    @patch(_MYSQL_CONNECT)
    def test_successful_connection(self, mock_connect):
        """connect_db returns a DialectConnection wrapping the raw conn."""
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = True
        mock_connect.return_value = mock_raw

        result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIsInstance(result, DialectConnection)
        mock_connect.assert_called_once_with(
            host="localhost", user="user", password="pass", database="mydb"
        )

    @patch(_MYSQL_CONNECT)
    def test_is_connected_false_returns_none(self, mock_connect):
        """connect_db returns None when is_connected() is False."""
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = False
        mock_connect.return_value = mock_raw

        result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIsNone(result)

    @patch(_MYSQL_CONNECT)
    def test_exception_returns_none(self, mock_connect):
        """connect_db returns None on exception."""
        mock_connect.side_effect = Exception("Access denied for user")

        result = connect_db("localhost", "user", "wrong_pass", "mydb")

        self.assertIsNone(result)

    @patch(_MYSQL_CONNECT)
    def test_success_logs_success_message(self, mock_connect):
        """connect_db logs a success message when connection is established."""
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = True
        mock_connect.return_value = mock_raw

        with self.assertLogs("dbbuddy_core.db", level="INFO") as logs:
            connect_db("localhost", "user", "pass", "mydb")
        self.assertIn("connected", "\n".join(logs.output).lower())

    @patch(_MYSQL_CONNECT)
    def test_is_connected_false_logs_error(self, mock_connect):
        """connect_db logs an error when is_connected() returns False."""
        mock_raw = MagicMock()
        mock_raw.is_connected.return_value = False
        mock_connect.return_value = mock_raw

        with self.assertLogs("dbbuddy_core.db", level="WARNING") as logs:
            connect_db("localhost", "user", "pass", "mydb")
        self.assertTrue(logs.output)

    @patch(_MYSQL_CONNECT)
    def test_exception_logs_failure_reason(self, mock_connect):
        """connect_db logs the exception message on failure."""
        mock_connect.side_effect = Exception("Unknown database 'mydb'")

        with self.assertLogs("dbbuddy_core.db", level="WARNING") as logs:
            connect_db("localhost", "user", "pass", "mydb")
        self.assertIn("Unknown database 'mydb'", "\n".join(logs.output))

    # Property: Exception-resilient connection
    @given(message=st.text(min_size=1))
    @settings(max_examples=100)
    @patch(_MYSQL_CONNECT)
    def test_property_exception_resilience(self, mock_connect, message):
        """For any exception, connect_db returns None and logs the error."""
        mock_connect.side_effect = Exception(message)

        with self.assertLogs("dbbuddy_core.db", level="WARNING") as logs:
            result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIsNone(result)
        self.assertIn(message, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
