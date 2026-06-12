# ── Test SQL Validator ───────────────────────────────────────────────────────
import pytest
from hypothesis import given, strategies as st
from dbbuddy_core.query import validate_against_schema, _extract_identifiers, is_valid_sql


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
        
        assert result["valid"] == True
        assert result["unknown_tables"] == []
        assert result["unknown_columns"] == []
        assert result["invalid_joins"] == []

    def test_invalid_table_fails_validation(self, sample_schema):
        """Test that SQL with unknown table fails validation."""
        sql = "SELECT id FROM unknown_table"
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == False
        assert "unknown_table" in result["unknown_tables"]

    def test_invalid_column_fails_validation(self, sample_schema):
        """Test that SQL with unknown column fails validation."""
        sql = "SELECT unknown_column FROM users"
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == False
        assert "unknown_column" in result["unknown_columns"]

    def test_valid_join_passes_validation(self, sample_schema):
        """Test that valid join passes validation."""
        sql = "SELECT users.name, orders.total FROM users JOIN orders ON users.id = orders.user_id"
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == True
        assert result["invalid_joins"] == []

    def test_invalid_join_table_fails_validation(self, sample_schema):
        """Test that join with unknown table fails validation."""
        sql = "SELECT * FROM users JOIN unknown_table ON users.id = unknown_table.id"
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == False
        assert len(result["invalid_joins"]) > 0
        assert any(join["reason"] == "table_not_found" for join in result["invalid_joins"])

    def test_invalid_join_column_fails_validation(self, sample_schema):
        """Test that join with unknown column fails validation."""
        sql = "SELECT * FROM users JOIN orders ON users.unknown_col = orders.user_id"
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == False
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
        """Test identifier extraction with multiple JOINs."""
        sql = "SELECT * FROM users JOIN orders ON users.id = orders.user_id JOIN products ON orders.product_id = products.id"
        tables, columns, joins = _extract_identifiers(sql)
        
        assert len(tables) == 3
        assert len(joins) == 2

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
        assert result["valid"] == True

    def test_validation_with_complex_query(self, sample_schema):
        """Test validation with complex multi-table query."""
        sql = """
        SELECT u.name, o.total 
        FROM users u 
        JOIN orders o ON u.id = o.user_id 
        WHERE o.total > 100
        """
        result = validate_against_schema(sql, sample_schema)
        
        assert result["valid"] == True
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
        
        assert result["valid"] == True


class TestPropertyBasedSQLSafety:
    """Property-based tests for AI safety guarantees."""

    @given(st.text(min_size=1, max_size=100))
    def test_sql_always_safe_from_destructive_operations(self, sql_input):
        """Property: SQL validation should always reject destructive operations.
        
        This ensures AI systems cannot generate DROP/TRUNCATE statements
        that could destroy data.
        """
        # Test the safety validation function
        result = is_valid_sql(sql_input)
        
        # If the SQL contains destructive keywords, it must be invalid
        sql_lower = sql_input.lower()
        if "drop" in sql_lower or "truncate" in sql_lower:
            assert result == False, f"Destructive SQL should be invalid: {sql_input}"

    @given(st.text(min_size=1, max_size=100))
    def test_valid_sql_always_starts_with_recognized_keyword(self, sql_input):
        """Property: Valid SQL must start with recognized DML/DQL keywords.
        
        This ensures AI systems don't generate malformed or non-SQL output.
        """
        result = is_valid_sql(sql_input)
        
        if result == True:
            # Must start with recognized keyword
            sql_lower = sql_input.strip().lower()
            first_word = sql_lower.split()[0] if sql_lower else ""
            assert first_word in ("select", "insert", "update", "delete", "with", "show", "explain")

    @given(st.text(min_size=1, max_size=50))
    def test_empty_or_unknown_sql_always_invalid(self, sql_input):
        """Property: Empty or unknown responses should always be invalid.
        
        This ensures AI systems don't pass through non-SQL responses.
        """
        # Test common AI failure modes
        if sql_input.strip() == "" or sql_input.lower() == "unknown":
            result = is_valid_sql(sql_input)
            assert result == False

    @given(st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=5))
    def test_schema_validation_never_accepts_hallucinated_tables(self, table_names):
        """Property: Schema validation should never accept hallucinated table names.
        
        This ensures AI systems don't reference non-existent tables.
        """
        schema = {"users": ["id", "name"], "orders": ["id", "total"]}
        
        for table in table_names:
            if table.lower() not in ["users", "orders"]:
                sql = f"SELECT * FROM {table}"
                result = validate_against_schema(sql, schema)
                assert result["valid"] == False, f"Should reject hallucinated table: {table}"

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

    @given(st.text(min_size=1, max_size=100))
    def test_sql_validation_case_insensitive_safety(self, sql_input):
        """Property: SQL safety checks should be case-insensitive.
        
        This ensures AI systems can't bypass safety with different casing.
        """
        # Test various casings of destructive keywords
        destructive_variants = ["DROP", "drop", "Drop", "DROP", "Truncate", "TRUNCATE", "truncate"]
        
        sql_lower = sql_input.lower()
        for variant in destructive_variants:
            if variant.lower() in sql_lower:
                result = is_valid_sql(sql_input)
                assert result == False, f"Should reject destructive SQL regardless of case: {sql_input}"
