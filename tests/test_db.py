import io
import sys
import unittest
from unittest.mock import patch, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from dbbuddy_core.db import connect_db


class TestConnectDb(unittest.TestCase):

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_successful_connection(self, mock_connect):
        """connect_db returns conn and prints success when is_connected() is True"""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_connect.return_value = mock_conn

        result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIs(result, mock_conn)
        mock_connect.assert_called_once_with(
            host="localhost", user="user", password="pass", database="mydb"
        )

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_is_connected_false_returns_none(self, mock_connect):
        """connect_db returns None and prints error when is_connected() is False"""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = False
        mock_connect.return_value = mock_conn

        result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIsNone(result)

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_exception_returns_none(self, mock_connect):
        """connect_db returns None and prints error message on exception"""
        mock_connect.side_effect = Exception("Access denied for user")

        result = connect_db("localhost", "user", "wrong_pass", "mydb")

        self.assertIsNone(result)

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_success_prints_success_message(self, mock_connect):
        """connect_db prints a success message when connection is established"""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_connect.return_value = mock_conn

        with patch("builtins.print") as mock_print:
            connect_db("localhost", "user", "pass", "mydb")
            printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
            self.assertIn("success", printed.lower())

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_is_connected_false_prints_error(self, mock_connect):
        """connect_db prints an error message when is_connected() returns False"""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = False
        mock_connect.return_value = mock_conn

        with patch("builtins.print") as mock_print:
            connect_db("localhost", "user", "pass", "mydb")
            printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
            self.assertTrue(len(printed) > 0)

    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_exception_prints_failure_reason(self, mock_connect):
        """connect_db prints the exception message on failure"""
        mock_connect.side_effect = Exception("Unknown database 'mydb'")

        with patch("builtins.print") as mock_print:
            connect_db("localhost", "user", "pass", "mydb")
            printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
            self.assertIn("Unknown database 'mydb'", printed)


    # Feature: db-buddy, Property 2: Exception-resilient connection
    # Validates: Requirements 2.4
    @given(message=st.text(min_size=1))
    @settings(max_examples=100)
    @patch("dbbuddy_core.db.mysql.connector.connect")
    def test_property_exception_resilience(self, mock_connect, message):
        """Property 2: For any exception type/message, connect_db returns None
        and prints an error message that includes the failure reason."""
        mock_connect.side_effect = Exception(message)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            result = connect_db("localhost", "user", "pass", "mydb")

        self.assertIsNone(result)
        self.assertIn(message, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
