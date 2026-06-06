import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch
from hypothesis import given, settings
from hypothesis import strategies as st
from dbbuddy.main import fetch_schema, local_classify, openai_classify, classify_column, ai_refine, batch_local_classify, batch_openai_classify, batch_classify_columns, load_config
from dbbuddy.plugins.loader import load_mapping_plugin


class TestMain(unittest.TestCase):
    """Unit tests for the main() CLI entry point — Requirements 1.1–1.5, 5.5, 5.6, 6.5, 6.6"""

    # ── Helper: build a minimal argv without --ai ────────────────────────────
    _base_argv = ["main.py"]

    # ── Test: --ai without OPENAI_API_KEY exits with code 1 ─────────────────
    def test_ai_flag_without_api_key_exits(self):
        """--ai flag with no OPENAI_API_KEY → print error and sys.exit(1)  (Req 6.6)"""
        from dbbuddy.main import main
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("sys.argv", ["main.py", "--ai"]):
                with patch("builtins.input", return_value="test"):
                    with patch("getpass.getpass", return_value="test"):
                        with self.assertRaises(SystemExit) as ctx:
                            main()
        self.assertEqual(ctx.exception.code, 1)

    def test_ai_flag_without_api_key_prints_error(self):
        """--ai flag with no OPENAI_API_KEY → error message is printed  (Req 6.6)"""
        from dbbuddy.main import main
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("sys.argv", ["main.py", "--ai", "--ai-provider", "openai"]):
                with patch("builtins.input", return_value="test"):
                    with patch("getpass.getpass", return_value="test"):
                        with patch("builtins.print") as mock_print:
                            with self.assertRaises(SystemExit):
                                main()
        mock_print.assert_called()
        # Check that the error message about OPENAI_API_KEY was printed
        error_printed = any("OPENAI_API_KEY" in str(call) for call in mock_print.call_args_list)
        self.assertTrue(error_printed)

    # ── Test: empty host defaults to "localhost" ──────────────────────────────
    def test_empty_host_uses_localhost_default(self):
        """Empty host input → "localhost" passed to connect_db  (Req 1.2)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["", "alice", "mydb"])  # host=empty, username, database
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="secret"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn) as mock_cd:
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", return_value="/tmp/output.json"):
                                with patch("builtins.print"):
                                    main()
        mock_cd.assert_called_once_with("localhost", "alice", "secret", "mydb")

    # ── Test: connect_db returns None → sys.exit(1) ───────────────────────────
    def test_connect_db_none_exits(self):
        """connect_db returns None → sys.exit(1)  (Req 1.5)"""
        from dbbuddy.main import main
        inputs = iter(["localhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=None):
                        with patch("builtins.print"):
                            with self.assertRaises(SystemExit) as ctx:
                                main()
        self.assertEqual(ctx.exception.code, 1)

    # ── Test: fetch_schema returns None → sys.exit(1) ─────────────────────────
    def test_fetch_schema_none_exits(self):
        """fetch_schema returns None → sys.exit(1)  (Req 5.5 / pipeline exit)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["localhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value=None):
                            with patch("builtins.print"):
                                with self.assertRaises(SystemExit) as ctx:
                                    main()
        self.assertEqual(ctx.exception.code, 1)

    # ── Test: write_output IOError → sys.exit(1) ─────────────────────────────
    def test_write_output_ioerror_exits(self):
        """write_output raises IOError → sys.exit(1)  (Req 5.5)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["localhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", side_effect=IOError("disk full")):
                                with patch("builtins.print"):
                                    with self.assertRaises(SystemExit) as ctx:
                                        main()
        self.assertEqual(ctx.exception.code, 1)

    # ── Test: write_output ValueError → sys.exit(1) ──────────────────────────
    def test_write_output_valueerror_exits(self):
        """write_output raises ValueError → sys.exit(1)  (Req 5.6)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["localhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", side_effect=ValueError("bad")):
                                with patch("builtins.print"):
                                    with self.assertRaises(SystemExit) as ctx:
                                        main()
        self.assertEqual(ctx.exception.code, 1)

    # ── Test: success path prints absolute path ───────────────────────────────
    def test_success_prints_output_path(self):
        """On success, print confirmation with the absolute path of output.json  (Req 5.4)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["myhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", return_value="/abs/path/output.json"):
                                with patch("builtins.print") as mock_print:
                                    main()
        # At least one print call should contain the path
        all_printed = " ".join(str(c[0][0]) for c in mock_print.call_args_list)
        self.assertIn("/abs/path/output.json", all_printed)

    # ── Test: empty username re-prompts until valid ───────────────────────────
    def test_empty_username_reprompts(self):
        """Empty username → re-prompt until non-empty value entered  (Req 1.3)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        # host, then 2 empty usernames, then valid username, then database
        call_responses = {
            "Host [localhost]: ": "localhost",
            "Username: ": "",  # first call returns empty
            "Database: ": "mydb",
        }
        call_counts = {"Username: ": 0}
        def fake_input(prompt):
            if prompt == "Username: ":
                call_counts["Username: "] += 1
                return "" if call_counts["Username: "] < 3 else "alice"
            return call_responses.get(prompt, "default")

        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=fake_input):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", return_value="/out.json"):
                                with patch("builtins.print"):
                                    main()
        # Username prompt should have been called 3 times (2 empty + 1 valid)
        self.assertEqual(call_counts["Username: "], 3)

    # ── Test: password prompt uses getpass.getpass, not input ────────────────
    def test_password_uses_getpass_not_input(self):
        """Password is collected via getpass.getpass(), not input()  (Req 1.2)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        inputs = iter(["localhost", "alice", "mydb"])
        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                with patch("getpass.getpass", return_value="secret") as mock_getpass:
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", return_value="/tmp/output.json"):
                                with patch("builtins.print"):
                                    main()
        # getpass.getpass must have been called exactly once
        mock_getpass.assert_called_once()
        # The call must NOT have gone through input() — verify input was only
        # called for host, username, and database (3 times total)
        # (if password used input, it would be a 4th call and StopIteration would fire)

    # ── Test: empty database re-prompts until valid ───────────────────────────
    def test_empty_database_reprompts(self):
        """Empty database name → re-prompt until non-empty value entered  (Req 1.3)"""
        from dbbuddy.main import main
        mock_conn = MagicMock()
        call_counts = {"Database: ": 0}
        def fake_input(prompt):
            if prompt == "Host [localhost]: ":
                return "localhost"
            if prompt == "Username: ":
                return "alice"
            if prompt == "Database: ":
                call_counts["Database: "] += 1
                return "" if call_counts["Database: "] < 2 else "mydb"
            return "default"

        with patch("sys.argv", self._base_argv):
            with patch("builtins.input", side_effect=fake_input):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn):
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.write_output", return_value="/out.json"):
                                with patch("builtins.print"):
                                    main()
        self.assertEqual(call_counts["Database: "], 2)


class TestFetchSchema(unittest.TestCase):

    def _make_conn(self, show_tables_rows, describe_map=None):
        """
        Build a mock connection whose cursor behaves as follows:
          - fetchall() after SHOW TABLES returns show_tables_rows
          - fetchall() after DESCRIBE <table> returns describe_map[table]
        describe_map: {table_name: [(col_name, ...), ...]}
        """
        cursor = MagicMock()
        describe_map = describe_map or {}

        # Track execute calls so we can route fetchall correctly
        fetchall_returns = []

        def execute_side_effect(sql):
            if sql == "SHOW TABLES":
                fetchall_returns.append(show_tables_rows)
            else:
                # e.g. "DESCRIBE users"
                table = sql.split()[-1]
                fetchall_returns.append(describe_map.get(table, []))

        cursor.execute.side_effect = execute_side_effect
        cursor.fetchall.side_effect = lambda: fetchall_returns.pop(0)

        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    # ── Test 1: empty-tables path ───────────────────────────────────────────
    def test_empty_database_returns_empty_dict(self):
        """SHOW TABLES returns no rows → fetch_schema returns {}  (Req 3.4)"""
        conn = self._make_conn(show_tables_rows=[])
        result = fetch_schema(conn)
        self.assertEqual(result, {})

    # ── Test 2: normal path with 2–3 tables ────────────────────────────────
    def test_normal_path_returns_correct_schema_dict(self):
        """SHOW TABLES returns 2 tables; DESCRIBE returns columns → correct dict  (Req 3.3)"""
        show_tables_rows = [("users",), ("orders",)]
        describe_map = {
            "users":  [("id",), ("name",), ("email",)],
            "orders": [("order_id",), ("amount",), ("status",)],
        }
        conn = self._make_conn(show_tables_rows, describe_map)
        result = fetch_schema(conn)

        self.assertEqual(set(result.keys()), {"users", "orders"})
        self.assertEqual(result["users"], ["id", "name", "email"])
        self.assertEqual(result["orders"], ["order_id", "amount", "status"])

    def test_normal_path_three_tables(self):
        """SHOW TABLES returns 3 tables; DESCRIBE returns columns → all keys present  (Req 3.3)"""
        show_tables_rows = [("products",), ("customers",), ("invoices",)]
        describe_map = {
            "products":  [("product_id",), ("title",), ("price",)],
            "customers": [("customer_id",), ("name",)],
            "invoices":  [("invoice_id",), ("total",), ("created_at",), ("status",)],
        }
        conn = self._make_conn(show_tables_rows, describe_map)
        result = fetch_schema(conn)

        self.assertEqual(set(result.keys()), {"products", "customers", "invoices"})
        self.assertEqual(result["products"], ["product_id", "title", "price"])
        self.assertEqual(result["customers"], ["customer_id", "name"])
        self.assertEqual(result["invoices"], ["invoice_id", "total", "created_at", "status"])

    # ── Test 3: exception on SHOW TABLES ───────────────────────────────────
    def test_show_tables_exception_returns_none_and_prints(self):
        """cursor.execute raises on SHOW TABLES → fetch_schema returns None and prints error  (Req 3.5)"""
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("SHOW TABLES failed")

        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch("builtins.print") as mock_print:
            result = fetch_schema(conn)

        self.assertIsNone(result)
        mock_print.assert_called_once()
        printed_msg = mock_print.call_args[0][0]
        self.assertIn("SHOW TABLES failed", printed_msg)

    # ── Test 4: exception on DESCRIBE ──────────────────────────────────────
    def test_describe_exception_returns_none_and_prints(self):
        """SHOW TABLES succeeds but DESCRIBE raises → fetch_schema returns None and prints error  (Req 3.5)"""
        cursor = MagicMock()
        call_count = {"n": 0}

        def execute_side_effect(sql):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call is SHOW TABLES — succeeds
                pass
            else:
                # Second call is DESCRIBE — raises
                raise Exception("DESCRIBE failed")

        cursor.execute.side_effect = execute_side_effect
        cursor.fetchall.return_value = [("users",)]  # one table from SHOW TABLES

        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch("builtins.print") as mock_print:
            result = fetch_schema(conn)

        self.assertIsNone(result)
        mock_print.assert_called_once()
        printed_msg = mock_print.call_args[0][0]
        self.assertIn("DESCRIBE failed", printed_msg)


# Feature: db-buddy, Property 3: DESCRIBE called once per table
class TestFetchSchemaProperty3(unittest.TestCase):

    @given(
        table_list=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"),
                min_size=1,
                max_size=32,
            ),
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    @settings(max_examples=100)
    def test_describe_called_exactly_once_per_table(self, table_list):
        """
        Property 3: For any list of N ≥ 1 table names returned by SHOW TABLES,
        fetch_schema shall call DESCRIBE exactly N times — once per table name.
        Validates: Requirements 3.2
        """
        cursor = MagicMock()

        # SHOW TABLES returns one row per table name
        show_tables_rows = [(name,) for name in table_list]

        fetchall_returns = []

        def execute_side_effect(sql):
            if sql == "SHOW TABLES":
                fetchall_returns.append(show_tables_rows)
            else:
                # DESCRIBE <table> — return a single dummy column row
                fetchall_returns.append([("col",)])

        cursor.execute.side_effect = execute_side_effect
        cursor.fetchall.side_effect = lambda: fetchall_returns.pop(0)

        conn = MagicMock()
        conn.cursor.return_value = cursor

        fetch_schema(conn)

        # Count how many execute calls contained "DESCRIBE"
        describe_calls = [
            c for c in cursor.execute.call_args_list
            if "DESCRIBE" in c.args[0]
        ]
        self.assertEqual(
            len(describe_calls),
            len(table_list),
            msg=(
                f"Expected {len(table_list)} DESCRIBE call(s) for tables "
                f"{table_list}, but got {len(describe_calls)}."
            ),
        )


# Feature: db-buddy, Property 4: Schema dict completeness and shape
class TestFetchSchemaProperty4(unittest.TestCase):

    @given(
        schema_spec=st.dictionaries(
            keys=st.text(
                alphabet=st.characters(
                    whitelist_categories=("Lu", "Ll", "Nd"),
                    whitelist_characters="_",
                ),
                min_size=1,
                max_size=32,
            ),
            values=st.lists(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Lu", "Ll", "Nd"),
                        whitelist_characters="_",
                    ),
                    min_size=1,
                    max_size=32,
                ),
                min_size=0,
                max_size=20,
            ),
            min_size=0,
            max_size=20,
        )
    )
    @settings(max_examples=100)
    def test_schema_dict_completeness_and_shape(self, schema_spec):
        """
        Property 4: For any database schema with N tables and M_i columns in
        table i, the dict returned by fetch_schema shall contain exactly N keys,
        and the value for each table shall be a list of exactly M_i column name
        strings matching the DESCRIBE output.
        Validates: Requirements 3.3
        """
        cursor = MagicMock()

        # Build SHOW TABLES rows and per-table DESCRIBE rows from schema_spec
        show_tables_rows = [(table,) for table in schema_spec]
        describe_map = {
            table: [(col,) for col in columns]
            for table, columns in schema_spec.items()
        }

        fetchall_returns = []

        def execute_side_effect(sql):
            if sql == "SHOW TABLES":
                fetchall_returns.append(show_tables_rows)
            else:
                table = sql.split()[-1]
                fetchall_returns.append(describe_map.get(table, []))

        cursor.execute.side_effect = execute_side_effect
        cursor.fetchall.side_effect = lambda: fetchall_returns.pop(0)

        conn = MagicMock()
        conn.cursor.return_value = cursor

        result = fetch_schema(conn)

        # The result must be a dict (not None)
        self.assertIsInstance(result, dict)

        # Exactly N keys — one per table
        self.assertEqual(
            len(result),
            len(schema_spec),
            msg=f"Expected {len(schema_spec)} keys, got {len(result)}.",
        )

        # Each table's value must be a list of exactly M_i column name strings
        for table, expected_columns in schema_spec.items():
            self.assertIn(
                table,
                result,
                msg=f"Table '{table}' missing from result dict.",
            )
            self.assertEqual(
                result[table],
                expected_columns,
                msg=(
                    f"Column list mismatch for table '{table}': "
                    f"expected {expected_columns}, got {result[table]}."
                ),
            )


# Feature: db-buddy, Property 5: Schema fetch exception returns None
class TestFetchSchemaProperty5(unittest.TestCase):

    @given(
        table_list=st.lists(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Lu", "Ll", "Nd"),
                    whitelist_characters="_",
                ),
                min_size=1,
                max_size=32,
            ),
            min_size=0,
            max_size=10,
            unique=True,
        ),
        # 0 means exception on SHOW TABLES; i >= 1 means exception on DESCRIBE call i
        fail_at=st.integers(min_value=0, max_value=10),
        exc_message=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=100)
    def test_schema_fetch_exception_returns_none_and_prints_error(
        self, table_list, fail_at, exc_message
    ):
        """
        Property 5: For any exception raised during SHOW TABLES or any DESCRIBE
        query, fetch_schema shall return None and print an error message
        indicating the failed operation.
        Validates: Requirements 3.5
        """
        cursor = MagicMock()
        call_count = {"n": 0}
        show_tables_rows = [(name,) for name in table_list]

        def execute_side_effect(sql):
            call_count["n"] += 1
            current = call_count["n"]
            # call 1 is SHOW TABLES; calls 2..N+1 are DESCRIBE for each table
            if fail_at == 0 and current == 1:
                # Raise on SHOW TABLES
                raise Exception(exc_message)
            # fail_at >= 1 means raise on the fail_at-th DESCRIBE call
            # DESCRIBE calls are at positions 2, 3, ... (current >= 2)
            # describe_index = current - 1 (1-based among DESCRIBE calls)
            describe_index = current - 1
            if fail_at >= 1 and describe_index == fail_at:
                raise Exception(exc_message)

        fetchall_returns = []

        def execute_and_store(sql):
            execute_side_effect(sql)
            if sql == "SHOW TABLES":
                fetchall_returns.append(show_tables_rows)
            else:
                fetchall_returns.append([("col",)])

        cursor.execute.side_effect = execute_and_store
        cursor.fetchall.side_effect = lambda: fetchall_returns.pop(0)

        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch("builtins.print") as mock_print:
            result = fetch_schema(conn)

        # Determine if the exception path should have been triggered:
        # - fail_at == 0: always raises on SHOW TABLES
        # - fail_at >= 1: raises only if there are enough tables (fail_at <= len(table_list))
        exception_should_trigger = (fail_at == 0) or (
            fail_at >= 1 and fail_at <= len(table_list)
        )

        if exception_should_trigger:
            # fetch_schema must return None
            self.assertIsNone(
                result,
                msg=(
                    f"Expected None when exception raised at step {fail_at} "
                    f"with {len(table_list)} tables, but got {result!r}."
                ),
            )
            # An error message must have been printed
            self.assertTrue(
                mock_print.called,
                msg=(
                    f"Expected print() to be called when exception raised at "
                    f"step {fail_at}, but it was not."
                ),
            )
        else:
            # No exception triggered — fetch_schema should return a dict normally
            self.assertIsInstance(
                result,
                dict,
                msg=(
                    f"Expected dict when no exception triggered (fail_at={fail_at}, "
                    f"tables={len(table_list)}), but got {result!r}."
                ),
            )


# Feature: db-buddy, Property 6: For any keyword K in HARDCODED_MAP and any casing permutation of K, map_column(K_variant) shall return the term associated with K via exact match
class TestMapColumnProperty6(unittest.TestCase):

    @given(
        keyword=st.sampled_from(list(__import__('dbbuddy.main', fromlist=['HARDCODED_MAP']).HARDCODED_MAP.keys())),
        casing_choices=st.lists(st.booleans(), min_size=0, max_size=50),
    )
    @settings(max_examples=100)
    def test_exact_match_priority_case_insensitive(self, keyword, casing_choices):
        """
        Property 6: For any keyword K in HARDCODED_MAP and any casing permutation
        of K, map_column(K_variant) shall return the term associated with K via
        exact match, not a substring match of another keyword.
        Validates: Requirements 4.2
        """
        from dbbuddy.main import HARDCODED_MAP, map_column

        # Build a casing-permuted variant of the keyword by toggling upper/lower
        # for each character using the generated boolean list (padded with False if shorter)
        variant_chars = []
        for i, ch in enumerate(keyword):
            use_upper = casing_choices[i] if i < len(casing_choices) else False
            variant_chars.append(ch.upper() if use_upper else ch.lower())
        k_variant = "".join(variant_chars)

        expected_term = HARDCODED_MAP[keyword]
        result = map_column(k_variant)

        self.assertEqual(
            result,
            expected_term,
            msg=(
                f"map_column({k_variant!r}) returned {result!r}, "
                f"but expected {expected_term!r} (from keyword {keyword!r})."
            ),
        )


class TestMapColumn(unittest.TestCase):
    """Unit tests for map_column — Requirements 4.2, 4.3, 4.4"""

    from dbbuddy.main import map_column

    # ── All 27 exact-match keywords ─────────────────────────────────────────
    def test_exact_match_all_27_keywords(self):
        """Every keyword in HARDCODED_MAP returns the correct term via exact match  (Req 4.2)"""
        from dbbuddy.main import HARDCODED_MAP, map_column
        for keyword, expected_term in HARDCODED_MAP.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(map_column(keyword), expected_term)

    # ── Mixed-case exact matches ─────────────────────────────────────────────
    def test_mixed_case_amt(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("AMT"), "value")

    def test_mixed_case_amount(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("Amount"), "value")

    def test_mixed_case_qty(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("QTY"), "quantity")

    def test_mixed_case_name(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("Name"), "name")

    def test_mixed_case_timestamp(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("TIMESTAMP"), "date")

    def test_mixed_case_uuid(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("UUID"), "identifier")

    def test_mixed_case_status(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("STATUS"), "status")

    def test_mixed_case_desc(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("DESC"), "description")

    # ── Substring-only matches ───────────────────────────────────────────────
    def test_substring_total_price(self):
        """`total_price` contains `price` (and `total`) — both map to "value"  (Req 4.3)"""
        from dbbuddy.main import map_column
        self.assertEqual(map_column("total_price"), "value")

    def test_substring_order_amount(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("order_amount"), "value")

    def test_substring_item_count(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("item_count"), "quantity")

    def test_substring_order_num(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("order_num"), "quantity")

    def test_substring_username(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("username"), "name")

    def test_substring_created_timestamp(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("created_timestamp"), "date")

    def test_substring_order_id(self):
        """`order_id` is not an exact match, but `id` is a substring → "identifier"  (Req 4.3)"""
        from dbbuddy.main import map_column
        self.assertEqual(map_column("order_id"), "identifier")

    # ── Longest-keyword-wins substring ──────────────────────────────────────
    def test_longest_keyword_wins_uuid_id(self):
        """`uuid_id` contains both `uuid` (4 chars) and `id` (2 chars); `uuid` wins  (Req 4.3)"""
        from dbbuddy.main import map_column
        self.assertEqual(map_column("uuid_id"), "identifier")

    def test_longest_keyword_wins_description_contains_desc(self):
        """`description` is an exact match; but also `desc` is a substring — exact match wins  (Req 4.2)"""
        from dbbuddy.main import map_column
        self.assertEqual(map_column("description"), "description")

    def test_longest_keyword_wins_number_contains_num(self):
        """`number` is exact; `num` is also a substring. Exact match takes priority  (Req 4.2)"""
        from dbbuddy.main import map_column
        self.assertEqual(map_column("number"), "quantity")

    def test_longest_keyword_wins_quantity_contains_qty_substring(self):
        """`item_quantity` substring: `quantity` (8 chars) wins over `qty` (3 chars)  (Req 4.3)"""
        from dbbuddy.main import map_column
        # "quantity" is a substring of "item_quantity" and is longer than "qty"
        self.assertEqual(map_column("item_quantity"), "quantity")

    # ── Fully unrecognized column names ──────────────────────────────────────
    def test_unrecognized_email(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("email"), "unknown")

    def test_unrecognized_address(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("address"), "unknown")

    def test_unrecognized_phone(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("phone"), "unknown")

    def test_unrecognized_xyz(self):
        from dbbuddy.main import map_column
        self.assertEqual(map_column("xyz"), "unknown")


class TestMapSchema(unittest.TestCase):
    """Unit tests for map_schema — Requirement 4.5"""

    def test_map_schema_known_two_table_schema(self):
        """map_schema returns correct Semantic_Layer shape for a known 2-table schema  (Req 4.5)"""
        from dbbuddy.main import map_schema

        schema = {
            "users":  ["id", "name", "email", "created_at"],
            "orders": ["order_id", "amount", "status", "updated_at"],
        }
        result = map_schema(schema)

        # Check structure
        self.assertEqual(set(result.keys()), {"users", "orders"})
        self.assertEqual(set(result["users"].keys()), {"id", "name", "email", "created_at"})
        self.assertEqual(set(result["orders"].keys()), {"order_id", "amount", "status", "updated_at"})
        
        # Check terms and sources
        self.assertEqual(result["users"]["id"]["term"], "identifier")
        self.assertEqual(result["users"]["id"]["source"], "rule")
        self.assertEqual(result["users"]["name"]["term"], "name")
        self.assertEqual(result["users"]["name"]["source"], "rule")
        self.assertEqual(result["users"]["email"]["term"], "unknown")
        self.assertEqual(result["users"]["email"]["source"], "rule")
        self.assertEqual(result["users"]["created_at"]["term"], "date")
        self.assertEqual(result["users"]["created_at"]["source"], "rule")
        
        self.assertEqual(result["orders"]["order_id"]["term"], "identifier")
        self.assertEqual(result["orders"]["order_id"]["source"], "rule")
        self.assertEqual(result["orders"]["amount"]["term"], "value")
        self.assertEqual(result["orders"]["amount"]["source"], "rule")
        self.assertEqual(result["orders"]["status"]["term"], "status")
        self.assertEqual(result["orders"]["status"]["source"], "rule")
        self.assertEqual(result["orders"]["updated_at"]["term"], "date")
        self.assertEqual(result["orders"]["updated_at"]["source"], "rule")

    def test_map_schema_empty_schema_returns_empty_dict(self):
        """map_schema on an empty schema returns {}  (Req 4.5)"""
        from dbbuddy.main import map_schema
        self.assertEqual(map_schema({}), {})

    def test_map_schema_all_columns_present(self):
        """Every table and column from the input schema is represented in the output  (Req 4.5)"""
        from dbbuddy.main import map_schema

        schema = {
            "products": ["product_id", "title", "price", "note"],
            "staff":    ["uuid", "label", "flag", "xyz"],
        }
        result = map_schema(schema)

        self.assertEqual(set(result.keys()), {"products", "staff"})
        self.assertEqual(set(result["products"].keys()), {"product_id", "title", "price", "note"})
        self.assertEqual(set(result["staff"].keys()), {"uuid", "label", "flag", "xyz"})

    def test_map_schema_correct_terms_for_all_columns(self):
        """Verify correct term assigned to each column in a mixed schema  (Req 4.5)"""
        from dbbuddy.main import map_schema

        schema = {
            "products": ["product_id", "title", "price", "note"],
            "staff":    ["uuid", "label", "flag", "xyz"],
        }
        result = map_schema(schema)

        self.assertEqual(result["products"]["product_id"]["term"], "identifier")
        self.assertEqual(result["products"]["product_id"]["source"], "rule")
        self.assertEqual(result["products"]["title"]["term"], "name")
        self.assertEqual(result["products"]["title"]["source"], "rule")
        self.assertEqual(result["products"]["price"]["term"], "value")
        self.assertEqual(result["products"]["price"]["source"], "rule")
        self.assertEqual(result["products"]["note"]["term"], "description")
        self.assertEqual(result["products"]["note"]["source"], "rule")
        self.assertEqual(result["staff"]["uuid"]["term"], "identifier")
        self.assertEqual(result["staff"]["uuid"]["source"], "rule")
        self.assertEqual(result["staff"]["label"]["term"], "name")
        self.assertEqual(result["staff"]["label"]["source"], "rule")
        self.assertEqual(result["staff"]["flag"]["term"], "status")
        self.assertEqual(result["staff"]["flag"]["source"], "rule")
        self.assertEqual(result["staff"]["xyz"]["term"], "unknown")
        self.assertEqual(result["staff"]["xyz"]["source"], "rule")

    def test_map_schema_table_with_no_columns(self):
        """map_schema handles a table with an empty column list gracefully  (Req 4.5)"""
        from dbbuddy.main import map_schema
        schema = {"empty_table": []}
        result = map_schema(schema)
        self.assertEqual(result, {"empty_table": {}})


# Feature: db-buddy, Property 7: Substring match — longest keyword wins

# Build the pairs at module load time so the strategy is available for @given
def _build_kw_pairs():
    from dbbuddy.main import HARDCODED_MAP
    keywords = list(HARDCODED_MAP.keys())
    pairs = []
    for i, kw_a in enumerate(keywords):
        for kw_b in keywords[i + 1:]:
            if HARDCODED_MAP[kw_a] == HARDCODED_MAP[kw_b]:
                continue
            if len(kw_a) == len(kw_b):
                continue
            if kw_a in kw_b or kw_b in kw_a:
                continue
            longer, shorter = (kw_a, kw_b) if len(kw_a) > len(kw_b) else (kw_b, kw_a)
            pairs.append((longer, shorter))
    return pairs


_KW_PAIRS = _build_kw_pairs()


class TestMapColumnProperty7Real(unittest.TestCase):

    @given(
        pair=st.sampled_from(_KW_PAIRS),
        prefix=st.text(
            alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
            min_size=0,
            max_size=6,
        ),
        middle=st.text(
            alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
            min_size=1,
            max_size=6,
        ),
        suffix=st.text(
            alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
            min_size=0,
            max_size=6,
        ),
    )
    @settings(max_examples=100)
    def test_longest_keyword_wins_in_substring_match(self, pair, prefix, middle, suffix):
        """
        Property 7: For any column name containing multiple HARDCODED_MAP keywords
        as substrings, map_column shall return the term for the longest matching
        keyword (not insertion-order first).
        Validates: Requirements 4.3
        """
        from dbbuddy.main import HARDCODED_MAP, map_column

        longer_kw, shorter_kw = pair

        # Build: prefix + longer_kw + middle + shorter_kw + suffix
        # This guarantees both keywords appear as substrings.
        col_name = prefix + longer_kw + middle + shorter_kw + suffix

        # The column name must NOT be an exact key in HARDCODED_MAP (to force the
        # substring-match branch).  If it happens to collide, skip this example.
        from hypothesis import assume
        assume(col_name not in HARDCODED_MAP)
        # Also skip if the lowercased form is an exact key (map_column lowercases first).
        assume(col_name.lower() not in HARDCODED_MAP)

        expected_term = HARDCODED_MAP[longer_kw]
        result = map_column(col_name)

        self.assertEqual(
            result,
            expected_term,
            msg=(
                f"map_column({col_name!r}) returned {result!r}, "
                f"but expected {expected_term!r} from longest keyword {longer_kw!r} "
                f"(shorter keyword present: {shorter_kw!r})."
            ),
        )


class TestMapColumnProperty8(unittest.TestCase):
    """
    # Feature: db-buddy, Property 8: Unmatched columns map to "unknown"
    For any column name string that contains no keyword from HARDCODED_MAP as a
    case-insensitive substring, map_column shall return "unknown".
    Validates: Requirements 4.4
    """

    @given(col_name=st.text(min_size=0, max_size=40))
    @settings(max_examples=100)
    def test_unmatched_column_returns_unknown(self, col_name):
        """
        Property 8: For any column name string that contains no keyword from
        HARDCODED_MAP as a case-insensitive substring, map_column shall return
        "unknown".
        Validates: Requirements 4.4
        """
        from hypothesis import assume
        from dbbuddy.main import HARDCODED_MAP, map_column

        normalized = col_name.lower()

        # Skip any string that contains a HARDCODED_MAP keyword as a substring
        # (case-insensitive) — those are not "unmatched".
        assume(not any(kw in normalized for kw in HARDCODED_MAP))

        result = map_column(col_name)

        self.assertEqual(
            result,
            "unknown",
            msg=(
                f"map_column({col_name!r}) returned {result!r}, "
                f"but expected 'unknown' because no HARDCODED_MAP keyword "
                f"appears as a substring in the lowercased form {normalized!r}."
            ),
        )


# Feature: db-buddy, Property 9: Semantic layer covers every column in the schema
class TestMapSchemaProperty9(unittest.TestCase):

    @given(
        schema=st.dictionaries(
            keys=st.text(
                alphabet=st.characters(
                    whitelist_categories=("Lu", "Ll", "Nd"),
                    whitelist_characters="_",
                ),
                min_size=1,
                max_size=32,
            ),
            values=st.lists(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Lu", "Ll", "Nd"),
                        whitelist_characters="_",
                    ),
                    min_size=1,
                    max_size=32,
                ),
                min_size=0,
                max_size=20,
            ),
            min_size=0,
            max_size=20,
        )
    )
    @settings(max_examples=100)
    def test_semantic_layer_covers_every_column(self, schema):
        """
        Property 9: For any schema dict, map_schema shall return a Semantic_Layer
        where every table in the schema is present and every column in each table
        is mapped to a term — no column is omitted.
        Validates: Requirements 4.5
        """
        from dbbuddy.main import map_schema

        result = map_schema(schema)

        # The result must be a dict
        self.assertIsInstance(result, dict)

        # Every table key from the schema must appear in the result
        self.assertEqual(
            set(result.keys()),
            set(schema.keys()),
            msg=(
                f"Table keys mismatch: schema has {set(schema.keys())}, "
                f"but Semantic_Layer has {set(result.keys())}."
            ),
        )

        # For each table, every column key must appear in the result
        for table, columns in schema.items():
            self.assertEqual(
                set(result[table].keys()),
                set(columns),
                msg=(
                    f"Column keys mismatch for table '{table}': "
                    f"schema has {set(columns)}, "
                    f"but Semantic_Layer has {set(result[table].keys())}."
                ),
            )


# Feature: db-buddy, Property 1: Required-field re-prompt loop terminates on valid input
class TestMainProperty1RequiredFieldReprompt(unittest.TestCase):
    """
    Property 1: For any sequence of N ≥ 0 empty strings followed by a non-empty string,
    the required-field prompt loop shall retry exactly N times and return the non-empty value.
    Validates: Requirements 1.3
    """

    @given(
        empties=st.lists(st.just(""), min_size=0, max_size=10),
        valid_value=st.text(min_size=1, max_size=50).filter(lambda s: s.strip() != ""),
    )
    @settings(max_examples=100)
    def test_username_reprompt_loop_terminates_on_valid_input(self, empties, valid_value):
        """
        Property 1 (username field): For N empty strings followed by a non-empty string,
        the username prompt loop retries exactly N times and returns the valid value
        (passed to connect_db as the username argument).
        Validates: Requirements 1.3
        """
        from dbbuddy.main import main

        # The sequence fed to the username prompt: N empties then one valid string
        username_inputs = empties + [valid_value]
        n = len(empties)

        # Track how many times the username prompt is called
        username_call_count = [0]

        def fake_input(prompt):
            if prompt == "Host [localhost]: ":
                return "localhost"
            elif prompt == "Username: ":
                idx = username_call_count[0]
                username_call_count[0] += 1
                return username_inputs[idx]
            elif prompt == "Database: ":
                return "testdb"
            return ""

        mock_conn = MagicMock()
        with patch("sys.argv", ["main.py"]):
            with patch("builtins.input", side_effect=fake_input):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn) as mock_cd:
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.map_schema", return_value={}):
                                with patch("dbbuddy.main.write_output", return_value="/out.json"):
                                    with patch("builtins.print"):
                                        main()

        # The username prompt must have been called exactly N+1 times
        self.assertEqual(
            username_call_count[0],
            n + 1,
            msg=(
                f"Expected username prompt to be called {n + 1} time(s) "
                f"(N={n} empty + 1 valid), but was called {username_call_count[0]} time(s)."
            ),
        )

        # The valid value must have been passed to connect_db as username
        called_username = mock_cd.call_args[0][1]
        self.assertEqual(
            called_username,
            valid_value.strip(),
            msg=(
                f"Expected connect_db to be called with username={valid_value.strip()!r}, "
                f"but got {called_username!r}."
            ),
        )

    @given(
        empties=st.lists(st.just(""), min_size=0, max_size=10),
        valid_value=st.text(min_size=1, max_size=50).filter(lambda s: s.strip() != ""),
    )
    @settings(max_examples=100)
    def test_database_reprompt_loop_terminates_on_valid_input(self, empties, valid_value):
        """
        Property 1 (database field): For N empty strings followed by a non-empty string,
        the database prompt loop retries exactly N times and returns the valid value
        (passed to connect_db as the database argument).
        Validates: Requirements 1.3
        """
        from dbbuddy.main import main

        # The sequence fed to the database prompt: N empties then one valid string
        database_inputs = empties + [valid_value]
        n = len(empties)

        # Track how many times the database prompt is called
        database_call_count = [0]

        def fake_input(prompt):
            if prompt == "Host [localhost]: ":
                return "localhost"
            elif prompt == "Username: ":
                return "alice"
            elif prompt == "Database: ":
                idx = database_call_count[0]
                database_call_count[0] += 1
                return database_inputs[idx]
            return ""

        mock_conn = MagicMock()
        with patch("sys.argv", ["main.py"]):
            with patch("builtins.input", side_effect=fake_input):
                with patch("getpass.getpass", return_value="pw"):
                    with patch("dbbuddy.main.connect_db", return_value=mock_conn) as mock_cd:
                        with patch("dbbuddy.main.fetch_schema", return_value={}):
                            with patch("dbbuddy.main.map_schema", return_value={}):
                                with patch("dbbuddy.main.write_output", return_value="/out.json"):
                                    with patch("builtins.print"):
                                        main()

        # The database prompt must have been called exactly N+1 times
        self.assertEqual(
            database_call_count[0],
            n + 1,
            msg=(
                f"Expected database prompt to be called {n + 1} time(s) "
                f"(N={n} empty + 1 valid), but was called {database_call_count[0]} time(s)."
            ),
        )

        # The valid value must have been passed to connect_db as the database argument
        called_database = mock_cd.call_args[0][3]
        self.assertEqual(
            called_database,
            valid_value.strip(),
            msg=(
                f"Expected connect_db to be called with database={valid_value.strip()!r}, "
                f"but got {called_database!r}."
            ),
        )


class TestWriteOutput(unittest.TestCase):
    """Unit tests for write_output — Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6"""

    # ── Test 1: 2-space indentation ─────────────────────────────────────────
    def test_two_space_indentation(self):
        """JSON is written with 2-space indentation  (Req 5.2)"""
        from dbbuddy.main import write_output
        import tempfile, os

        data = {"users": {"id": "identifier", "name": "name"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("os.path.abspath", return_value=os.path.join(tmpdir, "output.json")):
                write_output(data)

            output_file = os.path.join(tmpdir, "output.json")
            with open(output_file, "r") as f:
                content = f.read()

        # 2-space indent: second line should start with exactly 2 spaces
        lines = content.splitlines()
        # At least one non-first line should start with "  " (2 spaces) but not "    " (4)
        indented_lines = [l for l in lines[1:] if l.startswith("  ")]
        self.assertTrue(len(indented_lines) > 0, "Expected lines indented with 2 spaces")
        # Verify the indent is actually 2, not 4
        four_space_lines = [l for l in lines[1:] if l.startswith("    ") and not l.startswith("      ")]
        # Nested keys will be 4-space; top-level keys should be 2-space
        two_space_only = [l for l in lines[1:] if l.startswith("  ") and not l.startswith("    ")]
        self.assertTrue(len(two_space_only) > 0, "Expected top-level keys indented with exactly 2 spaces")

        # Also verify by re-parsing and re-dumping with indent=2 gives the same result
        import json
        expected = json.dumps(data, indent=2)
        self.assertEqual(content, expected)

    # ── Test 2: overwrite existing file ─────────────────────────────────────
    def test_overwrite_existing_file(self):
        """output.json already exists → overwritten silently without prompt  (Req 5.3)"""
        from dbbuddy.main import write_output
        import tempfile, os, json

        old_data = {"old": "data"}
        new_data = {"new": "data"}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output.json")

            # Write an existing file first
            with open(output_path, "w") as f:
                json.dump(old_data, f)

            with patch("os.path.abspath", return_value=output_path):
                write_output(new_data)

            with open(output_path, "r") as f:
                result = json.load(f)

        self.assertEqual(result, new_data)

    # ── Test 3: returns absolute path string on success ──────────────────────
    def test_returns_absolute_path_on_success(self):
        """write_output returns the absolute path string of output.json  (Req 5.4)"""
        from dbbuddy.main import write_output
        import tempfile, os

        data = {"table": {"col": "value"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            expected_path = os.path.join(tmpdir, "output.json")
            with patch("os.path.abspath", return_value=expected_path):
                result = write_output(data)

        self.assertEqual(result, expected_path)
        self.assertIsInstance(result, str)

    # ── Test 4: IOError propagates to caller ─────────────────────────────────
    def test_ioerror_propagates(self):
        """IOError during file write propagates to the caller  (Req 5.5)"""
        from dbbuddy.main import write_output
        import tempfile, os

        data = {"table": {"col": "value"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output.json")
            tmp_path = output_path + ".tmp"

            with patch("os.path.abspath", return_value=output_path):
                with patch("builtins.open", side_effect=IOError("disk full")):
                    with self.assertRaises(IOError) as ctx:
                        write_output(data)

        self.assertIn("disk full", str(ctx.exception))

    # ── Test 5: ValueError propagates (no partial file written) ──────────────
    def test_valueerror_propagates_no_partial_file(self):
        """ValueError from json.dump propagates; output.json is never created  (Req 5.6)"""
        from dbbuddy.main import write_output
        import tempfile, os, json

        # An object that is not JSON-serializable triggers ValueError
        class NotSerializable:
            pass

        bad_data = {"table": {"col": NotSerializable()}}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output.json")

            with patch("os.path.abspath", return_value=output_path):
                with self.assertRaises((ValueError, TypeError)):
                    write_output(bad_data)

            # output.json must NOT have been created
            self.assertFalse(
                os.path.exists(output_path),
                "output.json should not exist after a serialization failure",
            )
            # .tmp must also be cleaned up
            self.assertFalse(
                os.path.exists(output_path + ".tmp"),
                "output.json.tmp should be cleaned up after a serialization failure",
            )

    # ── Test 6: .tmp cleaned up on write error mid-write ─────────────────────
    def test_tmp_file_cleaned_up_on_write_error(self):
        """output.json.tmp is deleted when an IOError occurs during write  (Req 5.5)"""
        from dbbuddy.main import write_output
        import tempfile, os

        data = {"table": {"col": "value"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output.json")
            tmp_path = output_path + ".tmp"

            # Simulate open() succeeding but write raising mid-write by patching json.dump
            with patch("os.path.abspath", return_value=output_path):
                with patch("json.dump", side_effect=IOError("write failed mid-way")):
                    with self.assertRaises(IOError):
                        write_output(data)

            # .tmp must have been cleaned up
            self.assertFalse(
                os.path.exists(tmp_path),
                "output.json.tmp should be removed after a write error",
            )
            # output.json must not exist either
            self.assertFalse(
                os.path.exists(output_path),
                "output.json should not exist after a write error",
            )

    # ── Test 7: output.json absent when serialization fails (.tmp cleaned up) ─
    def test_output_json_absent_when_serialization_fails(self):
        """output.json is not created when json.dump raises; .tmp is cleaned up  (Req 5.6)"""
        from dbbuddy.main import write_output
        import tempfile, os

        data = {"table": {"col": "value"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output.json")
            tmp_path = output_path + ".tmp"

            with patch("os.path.abspath", return_value=output_path):
                with patch("json.dump", side_effect=ValueError("bad value")):
                    with self.assertRaises(ValueError):
                        write_output(data)

            # Both .tmp and final file must be absent
            self.assertFalse(
                os.path.exists(tmp_path),
                "output.json.tmp should be cleaned up after serialization error",
            )
            self.assertFalse(
                os.path.exists(output_path),
                "output.json should not exist after serialization error",
            )


class TestAIMapper(unittest.TestCase):
    """Unit tests for AI mapping functions — Requirement 6"""

    def setUp(self):
        """Clear AI cache before each test"""
        from dbbuddy.main import _ai_cache
        _ai_cache.clear()

    def test_local_classify_success(self):
        """Local Ollama classification returns valid term"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "response": "identifier"
        }
        
        with patch("dbbuddy.main.requests.post", return_value=mock_response):
            result = local_classify("email")
            self.assertEqual(result, "identifier")

    def test_local_classify_timeout_returns_unknown(self):
        """Local Ollama timeout returns unknown"""
        with patch("dbbuddy.main.requests.post", side_effect=Exception("Timeout")):
            result = local_classify("email")
            self.assertEqual(result, "unknown")

    def test_openai_classify_success(self):
        """OpenAI classification returns valid term (Req 6.2)"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "identifier"}}]
        }
        
        with patch("dbbuddy.main.requests.post", return_value=mock_response):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                result = openai_classify("email")
                self.assertEqual(result, "identifier")

    def test_openai_classify_timeout_returns_unknown(self):
        """OpenAI timeout returns unknown (Req 6.3)"""
        with patch("dbbuddy.main.requests.post", side_effect=Exception("Timeout")):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                result = openai_classify("email")
                self.assertEqual(result, "unknown")

    def test_classify_column_local_provider(self):
        """classify_column routes to local provider"""
        with patch("dbbuddy.main.local_classify", return_value="identifier"):
            result = classify_column("email", "local")
            self.assertEqual(result, "identifier")

    def test_classify_column_openai_provider(self):
        """classify_column routes to OpenAI provider"""
        with patch("dbbuddy.main.openai_classify", return_value="identifier"):
            result = classify_column("email", "openai")
            self.assertEqual(result, "identifier")

    def test_classify_column_hybrid_fallback(self):
        """Hybrid provider falls back to OpenAI when local returns unknown"""
        with patch("dbbuddy.main.local_classify", return_value="unknown"):
            with patch("dbbuddy.main.openai_classify", return_value="value"):
                result = classify_column("amount", "hybrid")
                self.assertEqual(result, "value")

    def test_classify_column_hybrid_no_fallback(self):
        """Hybrid provider uses local result when successful"""
        with patch("dbbuddy.main.local_classify", return_value="identifier"):
            with patch("dbbuddy.main.openai_classify", return_value="value") as mock_openai:
                result = classify_column("email", "hybrid")
                self.assertEqual(result, "identifier")
                # OpenAI should not be called
                mock_openai.assert_not_called()

    def test_classify_column_invalid_provider(self):
        """Invalid provider returns unknown"""
        result = classify_column("email", "invalid")
        self.assertEqual(result, "unknown")

    def test_ai_refine_with_provider(self):
        """ai_refine uses provider parameter"""
        semantic_layer = {
            "users": {
                "id": {"term": "identifier", "source": "rule"},
                "email": {"term": "unknown", "source": "rule"},
                "name": {"term": "name", "source": "rule"}
            }
        }
        
        with patch("dbbuddy.main.batch_classify_columns", return_value={"email": "contact"}):
            result = ai_refine(semantic_layer, "openai")
            self.assertEqual(result["users"]["email"]["term"], "contact")
            self.assertEqual(result["users"]["email"]["source"], "openai")

    def test_ai_refine_logs_batch_classification(self):
        """ai_refine logs batch classification events"""
        semantic_layer = {
            "users": {
                "id": {"term": "identifier", "source": "rule"},
                "email": {"term": "unknown", "source": "rule"},
                "name": {"term": "name", "source": "rule"}
            }
        }
        
        with patch("dbbuddy.main.batch_classify_columns", return_value={"email": "contact"}):
            with patch("dbbuddy.main.logger") as mock_logger:
                ai_refine(semantic_layer, "openai")
                # Verify batch logging calls
                mock_logger.info.assert_any_call("[openai] Batch classifying 1 columns")
                mock_logger.info.assert_any_call("[openai] Batch classification completed")

    def test_openai_classify_caching(self):
        """Cache prevents repeated API calls for same column"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "identifier"}}]
        }
        
        with patch("dbbuddy.main.requests.post", return_value=mock_response) as mock_post:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                result1 = openai_classify("email")
                result2 = openai_classify("email")
                self.assertEqual(result1, "identifier")
                self.assertEqual(result2, "identifier")
                # Should only call API once due to caching
                self.assertEqual(mock_post.call_count, 1)

    def test_batch_local_classify_success(self):
        """Batch local classification returns valid mapping"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "response": '{"email": "identifier", "phone": "contact"}'
        }
        
        with patch("dbbuddy.main.requests.post", return_value=mock_response):
            result = batch_local_classify(["email", "phone"])
            self.assertEqual(result["email"], "identifier")
            self.assertEqual(result["phone"], "contact")

    def test_batch_local_classify_timeout_returns_unknown(self):
        """Batch local timeout returns unknown for all columns"""
        with patch("dbbuddy.main.requests.post", side_effect=Exception("Timeout")):
            result = batch_local_classify(["email", "phone"])
            self.assertEqual(result["email"], "unknown")
            self.assertEqual(result["phone"], "unknown")

    def test_batch_openai_classify_success(self):
        """Batch OpenAI classification returns valid mapping"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"email": "identifier", "phone": "contact"}'}}]
        }
        
        with patch("dbbuddy.main.requests.post", return_value=mock_response):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                result = batch_openai_classify(["email", "phone"])
                self.assertEqual(result["email"], "identifier")
                self.assertEqual(result["phone"], "contact")

    def test_batch_classify_columns_local_provider(self):
        """Batch classify routes to local provider"""
        with patch("dbbuddy.main.batch_local_classify", return_value={"email": "identifier"}):
            result = batch_classify_columns(["email"], "local")
            self.assertEqual(result["email"], "identifier")

    def test_batch_classify_columns_openai_provider(self):
        """Batch classify routes to OpenAI provider"""
        with patch("dbbuddy.main.batch_openai_classify", return_value={"email": "identifier"}):
            result = batch_classify_columns(["email"], "openai")
            self.assertEqual(result["email"], "identifier")

    def test_batch_classify_columns_hybrid_fallback(self):
        """Batch hybrid falls back to OpenAI for unknowns"""
        with patch("dbbuddy.main.batch_local_classify", return_value={"email": "unknown"}):
            with patch("dbbuddy.main.batch_openai_classify", return_value={"email": "identifier"}):
                result = batch_classify_columns(["email"], "hybrid")
                self.assertEqual(result["email"], "identifier")

    def test_ai_refine_uses_batch_processing(self):
        """ai_refine uses batch processing instead of individual calls"""
        semantic_layer = {
            "users": {
                "id": {"term": "identifier", "source": "rule"},
                "email": {"term": "unknown", "source": "rule"},
                "phone": {"term": "unknown", "source": "rule"}
            }
        }
        
        with patch("dbbuddy.main.batch_classify_columns", return_value={"email": "contact", "phone": "contact"}):
            result = ai_refine(semantic_layer, "openai")
            self.assertEqual(result["users"]["email"]["term"], "contact")
            self.assertEqual(result["users"]["email"]["source"], "openai")
            self.assertEqual(result["users"]["phone"]["term"], "contact")
            self.assertEqual(result["users"]["phone"]["source"], "openai")

    def test_ai_refine_empty_unknown_columns(self):
        """ai_refine returns early when no unknown columns"""
        semantic_layer = {
            "users": {
                "id": {"term": "identifier", "source": "rule"},
                "name": {"term": "name", "source": "rule"}
            }
        }
        
        with patch("dbbuddy.main.batch_classify_columns") as mock_batch:
            result = ai_refine(semantic_layer, "openai")
            # Batch should not be called when no unknown columns
            mock_batch.assert_not_called()
            self.assertEqual(result, semantic_layer)


class TestConfigSystem(unittest.TestCase):
    """Unit tests for configuration system"""

    def test_load_config_no_flag_returns_empty(self):
        """load_config returns empty dict when --config flag not present"""
        with patch("sys.argv", ["main.py"]):
            result = load_config()
            self.assertEqual(result, {})

    def test_load_config_missing_file_exits(self):
        """load_config exits when config file not found"""
        with patch("sys.argv", ["main.py", "--config", "nonexistent.json"]):
            with self.assertRaises(SystemExit):
                load_config()

    def test_load_config_invalid_json_exits(self):
        """load_config exits when config file has invalid JSON"""
        with patch("sys.argv", ["main.py", "--config", "invalid.json"]):
            with patch("builtins.open", side_effect=FileNotFoundError):
                with self.assertRaises(SystemExit):
                    load_config()

    def test_load_config_valid_json_returns_config(self):
        """load_config returns config dict when valid JSON file exists"""
        test_config = {"host": "localhost", "user": "root", "database": "testdb"}
        with patch("sys.argv", ["main.py", "--config", "config.json"]):
            with patch("builtins.open", MagicMock()) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = json.dumps(test_config)
                result = load_config()
                self.assertEqual(result["host"], "localhost")
                self.assertEqual(result["user"], "root")
                self.assertEqual(result["database"], "testdb")


class TestPluginLoader(unittest.TestCase):
    """Unit tests for plugin loader"""

    def test_load_valid_plugin(self):
        """load_mapping_plugin returns instance for valid plugin"""
        from dbbuddy.plugins.default_mapping import Plugin
        plugin = load_mapping_plugin("default_mapping")
        self.assertIsInstance(plugin, Plugin)

    def test_load_invalid_plugin_fallback(self):
        """load_mapping_plugin falls back to Plugin for invalid plugin"""
        from dbbuddy.plugins.default_mapping import Plugin
        plugin = load_mapping_plugin("nonexistent_plugin")
        self.assertIsInstance(plugin, Plugin)

    def test_plugin_classify_works(self):
        """Plugin classify method works correctly"""
        plugin = load_mapping_plugin("default_mapping")
        self.assertEqual(plugin.classify("amount"), "value")
        self.assertEqual(plugin.classify("qty"), "quantity")
        self.assertEqual(plugin.classify("unknown_column"), "unknown")


if __name__ == "__main__":
    unittest.main()
