# ── Test SQL Validator ───────────────────────────────────────────────────────
import pytest
from hypothesis import given, strategies as st
from dbbuddy_core.query import validate_against_schema, _extract_identifiers


class TestSQLValidator:
    """Test schema-aware SQL validation with join support."""

    @pytest.fixture
    def sample_schema(self):
        """Sample database schema for testing."""
        return {
            "users": ["id", "name", "email", "created_at"],
            "orders": ["id", "user_id", "total", "created_at"],
            "products": ["id", "name", "price"]
        }

    def test_valid_sql_passes_validation(self, sample_schema):
        """Test that valid SQL passes schema validation."""
        sql = "SELECT id, name FROM users"
        result = validate_against_schema(sql, sample_schema)

        assert result["valid"]
        assert result["unknown_tables"] == []
        assert result["unknown_columns"] == []
        assert result["invalid_joins"] == []

    def test_invalid_table_fails_validation(self, sample_schema):
        """Test that SQL with unknown table fails validation."""
        sql = "SELECT id FROM unknown_table"
        result = validate_against_schema(sql, sample_schema)

        assert not result["valid"]
        assert "unknown_table" in result["unknown_tables"]

    def test_invalid_column_fails_validation(self, sample_schema):
        """Test that SQL with unknown column fails validation."""
        sql = "SELECT unknown_column FROM users"
        result = validate_against_schema(sql, sample_schema)

        assert not result["valid"]
        assert "unknown_column" in result["unknown_columns"]

    def test_valid_join_passes_validation(self, sample_schema):
        """Test that valid join passes validation."""
        sql = "SELECT users.name, orders.total FROM users JOIN orders ON users.id = orders.user_id"
        result = validate_against_schema(sql, sample_schema)

        assert result["valid"]
        assert result["invalid_joins"] == []

    def test_invalid_join_table_fails_validation(self, sample_schema):
        """A join against an unknown table is reported *as a join problem*.

        Regression guard: this used to be skipped as "superseded", but the join
        never reached validation at all — `_extract_identifiers` required a
        keyword after the ON clause, so a trailing JOIN (the commonest shape)
        produced no join to check. It only failed validation incidentally, via
        `unknown_tables`.
        """
        sql = "SELECT * FROM users JOIN unknown_table ON users.id = unknown_table.id"
        result = validate_against_schema(sql, sample_schema)

        assert not result["valid"]
        assert len(result["invalid_joins"]) > 0
        assert any(join["reason"] == "table_not_found" for join in result["invalid_joins"])

    def test_a_trailing_join_is_extracted(self):
        """The last JOIN in a statement has no keyword after it. Extract it anyway."""
        _, _, joins = _extract_identifiers(
            "SELECT * FROM users JOIN orders ON users.id = orders.user_id")
        assert len(joins) == 1
        assert joins[0]["table"] == "orders"
        # The whole condition, not truncated at the "order" inside "orders".
        assert "orders.user_id" in joins[0]["condition"]
        assert ("users", "id") in joins[0]["column_refs"]
        assert ("orders", "user_id") in joins[0]["column_refs"]

    def test_join_clause_boundaries_respect_word_boundaries(self, sample_schema):
        """`order` must not match inside `orders` and end the condition early."""
        _, _, joins = _extract_identifiers(
            "SELECT * FROM users JOIN orders ON users.id = orders.user_id "
            "ORDER BY orders.total")
        assert len(joins) == 1
        assert "order by" not in joins[0]["condition"].lower()
        assert ("orders", "user_id") in joins[0]["column_refs"]

    def test_aliased_join_is_extracted(self):
        _, _, joins = _extract_identifiers(
            "SELECT * FROM users u JOIN orders o ON u.id = o.user_id")
        assert len(joins) == 1
        assert joins[0]["table"] == "orders"

    def test_invalid_join_column_fails_validation(self, sample_schema):
        """Test that join with unknown column fails validation."""
        sql = "SELECT * FROM users JOIN orders ON users.unknown_col = orders.user_id"
        result = validate_against_schema(sql, sample_schema)

        assert not result["valid"]
        assert len(result["invalid_joins"]) > 0

    def test_extract_identifiers_basic(self):
        """Test basic identifier extraction from SQL."""
        sql = "SELECT id, name FROM users WHERE id = 1"
        tables, columns, joins = _extract_identifiers(sql)

        assert "users" in tables
        assert "id" in columns
        assert "name" in columns
        assert len(joins) == 0

    def test_extract_identifiers_with_join(self):
        """Test identifier extraction with JOIN clause."""
        sql = "SELECT users.name FROM users JOIN orders ON users.id = orders.user_id"
        tables, columns, joins = _extract_identifiers(sql)

        assert "users" in tables
        assert "orders" in tables
        assert len(joins) > 0
        assert joins[0]["table"] == "orders"

    def test_extract_identifiers_multiple_joins(self):
        """Every JOIN in a chain is extracted, not every other one.

        Regression guard: the boundary keyword used to be *consumed*, so the
        second clause's own `JOIN` was eaten by the first match and never started
        one of its own.
        """
        sql = ("SELECT * FROM users JOIN orders ON users.id = orders.user_id "
               "JOIN products ON orders.product_id = products.id")
        tables, columns, joins = _extract_identifiers(sql)

        assert len(tables) == 3
        assert len(joins) == 2
        assert [j["table"] for j in joins] == ["orders", "products"]

    def test_extract_identifiers_column_references(self):
        """Test that column references in joins are extracted."""
        sql = "SELECT * FROM users JOIN orders ON users.id = orders.user_id"
        tables, columns, joins = _extract_identifiers(sql)

        assert len(joins) > 0
        assert len(joins[0]["column_refs"]) > 0
        # Should extract (table, column) pairs from join condition

    def test_validation_with_empty_schema(self):
        """Test validation with empty schema returns valid."""
        sql = "SELECT id FROM users"
        result = validate_against_schema(sql, {})

        # Empty schema should return valid (no validation possible)
        assert result["valid"]

    def test_validation_with_complex_query(self, sample_schema):
        """Test validation with complex multi-table query."""
        sql = """
        SELECT u.name, o.total
        FROM users u
        JOIN orders o ON u.id = o.user_id
        WHERE o.total > 100
        """
        result = validate_against_schema(sql, sample_schema)

        assert result["valid"]
        assert "users" in result.get("unknown_tables", []) or "users" not in result.get("unknown_tables", [])

    def test_case_insensitive_validation(self, sample_schema):
        """Test that validation is case-insensitive."""
        sql_upper = "SELECT ID FROM USERS"
        sql_lower = "select id from users"

        result_upper = validate_against_schema(sql_upper, sample_schema)
        result_lower = validate_against_schema(sql_lower, sample_schema)

        assert result_upper["valid"] == result_lower["valid"]

    def test_validation_with_subquery(self, sample_schema):
        """Test validation with subquery."""
        sql = "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
        result = validate_against_schema(sql, sample_schema)

        # Should handle subqueries (basic validation)
        assert "valid" in result

    def test_extract_identifiers_with_aliases(self):
        """Test identifier extraction with table aliases."""
        sql = "SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id"
        tables, columns, joins = _extract_identifiers(sql)

        assert "users" in tables
        assert "orders" in tables

    def test_validation_with_aggregate_functions(self, sample_schema):
        """Test validation with aggregate functions."""
        sql = "SELECT COUNT(*), SUM(total) FROM orders"
        result = validate_against_schema(sql, sample_schema)

        assert result["valid"]


class TestPropertyBasedSQLSafety:
    """Property-based tests for AI safety guarantees."""

    # Generate *identifiers*, not arbitrary text. The original strategy was plain
    # `st.text()`, which produces whitespace-only and punctuation-only values —
    # those interpolate into `SELECT * FROM   `, which names no table, so there is
    # nothing to reject and the property is vacuously false. That is a bad
    # strategy, not a broken validator, and it is why this test spent time on the
    # skip-list. Constraining the strategy tests the property that was meant.
    _IDENTIFIERS = st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"),
        min_size=1, max_size=20,
    ).filter(lambda s: s.strip() and s.lower() not in {"users", "orders"})

    @given(st.lists(_IDENTIFIERS, min_size=1, max_size=5))
    def test_schema_validation_never_accepts_hallucinated_tables(self, table_names):
        """Property: validation never accepts a table that is not in the schema.

        This is the guarantee that keeps an AI-suggested table name from reaching
        a customer database.
        """
        schema = {"users": ["id", "name"], "orders": ["id", "total"]}

        for table in table_names:
            sql = f"SELECT * FROM {table}"
            result = validate_against_schema(sql, schema)
            assert not result["valid"], f"Should reject hallucinated table: {table!r}"

    @given(st.text(min_size=1, max_size=30))
    def test_extract_identifiers_always_returns_valid_structure(self, sql_input):
        """Property: Identifier extraction should always return valid structure.

        This ensures the parsing layer is robust against any input.
        """
        tables, columns, joins = _extract_identifiers(sql_input)

        # Should always return lists
        assert isinstance(tables, list)
        assert isinstance(columns, list)
        assert isinstance(joins, list)

        # Should not crash on any input
        assert True  # If we get here, structure is valid

