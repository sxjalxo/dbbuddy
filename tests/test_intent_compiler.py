# ── Test Intent Compiler ─────────────────────────────────────────────────────
import pytest
from hypothesis import given, strategies as st
from dbbuddy_core.query import compile_sql_from_intent


@pytest.fixture
def sample_schema():
    """Sample database schema for testing."""
    return {
        "users": ["id", "name", "email", "created_at"],
        "orders": ["id", "user_id", "total", "created_at"],
        "products": ["id", "name", "price"]
    }

@pytest.fixture
def sample_relationships():
    """Sample relationship graph for testing."""
    return {
        "orders": {
            "user_id": ("users", "id")
        },
        "products": {}
    }


class TestIntentCompiler:
    """Test deterministic SQL compilation from intent structure."""

    def test_compile_simple_intent(self, sample_schema):
        """Test compilation of simple single-table intent."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert sql == "SELECT id, name FROM users;"

    def test_compile_intent_with_all_columns(self, sample_schema):
        """Test compilation when no specific columns specified (SELECT *)."""
        intent = {
            "tables": ["users"],
            "columns": [],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert sql == "SELECT * FROM users;"

    def test_compile_intent_with_filters(self, sample_schema):
        """Test compilation with WHERE clause filters."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": ["id = 1"]
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "SELECT id, name FROM users" in sql
        assert "WHERE" in sql
        assert "id = 1" in sql

    def test_compile_intent_with_multiple_filters(self, sample_schema):
        """Test compilation with multiple filters."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": ["id = 1", "name = 'test'"]
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "WHERE" in sql
        assert "AND" in sql

    def test_compile_intent_with_invalid_columns(self, sample_schema):
        """Test compilation filters out invalid columns."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "invalid_column", "name"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        # Should only include valid columns
        assert "invalid_column" not in sql
        assert "id" in sql
        assert "name" in sql

    def test_compile_intent_with_joins(self, sample_schema, sample_relationships):
        """Test compilation with JOIN using relationship graph."""
        intent = {
            "tables": ["users", "orders"],
            "columns": ["id", "total"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema, sample_relationships)
        
        assert "JOIN" in sql
        assert "orders" in sql
        assert "ON" in sql

    def test_compile_intent_without_relationships(self, sample_schema):
        """Test compilation without relationship graph (no joins)."""
        intent = {
            "tables": ["users", "orders"],
            "columns": ["id", "total"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        # Should return failure sentinel when relationships is not provided and confidence is low
        assert sql == "SELECT * FROM unknown;"

    def test_compile_empty_intent(self, sample_schema):
        """Test compilation with empty intent."""
        intent = {}
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "unknown" in sql

    def test_compile_intent_without_tables(self, sample_schema):
        """Test compilation when intent has no tables."""
        intent = {
            "columns": ["id", "name"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "unknown" in sql

    def test_compile_intent_with_invalid_table(self, sample_schema):
        """Test compilation with table not in schema."""
        intent = {
            "tables": ["unknown_table"],
            "columns": ["id"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        # Should still generate SQL even if table not in schema (fallback)
        assert "unknown_table" in sql

    def test_compile_intent_case_sensitivity(self, sample_schema):
        """Test that column names are handled case-insensitively."""
        intent = {
            "tables": ["users"],
            "columns": ["ID", "NAME"],  # uppercase
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        # Should match lowercase columns in schema
        assert "ID" in sql or "id" in sql

    def test_compile_intent_with_complex_filters(self, sample_schema):
        """Test compilation with complex filter expressions."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": ["id > 10", "name LIKE '%test%'"]
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "WHERE" in sql
        assert "id > 10" in sql
        assert "name LIKE '%test%'" in sql

    def test_compile_intent_preserves_order(self, sample_schema):
        """Test that column order is preserved from intent."""
        intent = {
            "tables": ["users"],
            "columns": ["name", "email", "id"],  # specific order
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        # Should preserve the order
        assert sql.index("name") < sql.index("email")
        assert sql.index("email") < sql.index("id")

    def test_compile_intent_with_single_column(self, sample_schema):
        """Test compilation with single column."""
        intent = {
            "tables": ["users"],
            "columns": ["id"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert sql == "SELECT id FROM users;"

    def test_compile_intent_with_no_filters(self, sample_schema):
        """Test compilation without WHERE clause."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "WHERE" not in sql

    def test_compile_intent_deterministic_output(self, sample_schema):
        """Test that same intent always produces same SQL (deterministic)."""
        intent = {
            "tables": ["users"],
            "columns": ["id", "name"],
            "filters": ["id = 1"]
        }
        
        sql1 = compile_sql_from_intent(intent, sample_schema)
        sql2 = compile_sql_from_intent(intent, sample_schema)
        
        assert sql1 == sql2

    def test_compile_intent_with_implicit_wildcard(self, sample_schema):
        """Test that empty columns list produces wildcard."""
        intent = {
            "tables": ["users"],
            "columns": [],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, sample_schema)
        
        assert "*" in sql

    def test_confidence_scoring_fallback(self, sample_schema):
        """Test that confidence scoring fallback triggers when confidence < 0.5."""
        intent = {
            "tables": ["users", "products"],  # no relationships between them in sample_relationships
            "columns": ["id"],
            "filters": []
        }
        relationships = {"orders": {"user_id": ("users", "id")}}
        sql = compile_sql_from_intent(intent, sample_schema, relationships)
        assert sql == "SELECT * FROM unknown;"

    def test_bidirectional_join_path_bfs(self, sample_schema):
        """Test bidirectional join path BFS that connects referencing to referenced and vice-versa."""
        relationships = {
            "orders": {
                "user_id": ("users", "id"),
                "product_id": ("products", "id")
            }
        }
        intent = {
            "tables": ["products", "users"],
            "columns": ["name"],
            "filters": []
        }
        sql = compile_sql_from_intent(intent, sample_schema, relationships)
        assert "JOIN orders" in sql
        assert "ON" in sql

    def test_aggregation_terms_robustness(self, sample_schema):
        """Test robust aggregation keyword detection list (revenue, avg, sales, generated)."""
        intent = {
            "tables": ["users"],
            "columns": [],
            "filters": []
        }
        # wants_sum due to 'revenue'
        sql_sum = compile_sql_from_intent(intent, sample_schema, user_query="Show total revenue for users")
        assert "SUM(" in sql_sum or "COUNT(" in sql_sum

        # wants_avg due to 'average'
        sql_avg = compile_sql_from_intent(intent, sample_schema, user_query="What is the average price of products")
        assert "AVG(" in sql_avg or "COUNT(" in sql_avg

    def test_composable_accumulative_where_filters(self):
        """Test that filters (time, country, status, numeric) accumulate composably."""
        custom_schema = {
            "users": ["id", "name", "email", "country", "status", "created_at"]
        }
        intent = {
            "tables": ["users"],
            "columns": ["id"],
            "filters": []
        }
        # India (country) + active (status) + last month (time)
        sql = compile_sql_from_intent(
            intent, 
            custom_schema, 
            user_query="users from india who are active from last month"
        )
        assert "WHERE" in sql
        assert "country = 'India'" in sql
        assert "status = 'active'" in sql
        assert "created_at >= DATE_SUB(CURDATE()" in sql


class TestIntentCompilerFuzzing:
    """Fuzz testing for intent compiler resilience."""

    @given(st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=5),
        min_size=0, max_size=5
    ))
    def test_random_intent_never_crashes(self, intent_dict):
        """Property: Random intent should never crash the compiler.
        
        This tests resilience under garbage input and ensures the compiler
        handles unexpected data structures gracefully.
        """
        schema = {"users": ["id", "name", "email"]}
        
        try:
            sql = compile_sql_from_intent(intent_dict, schema)
            # Should always return a string
            assert isinstance(sql, str)
        except Exception:
            # Should not crash on any input
            pytest.fail("Compiler crashed on random intent")

    @given(st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=10),
        min_size=0, max_size=3
    ))
    def test_random_intent_always_returns_string(self, intent_dict):
        """Property: Compiler should always return string regardless of input."""
        schema = {"users": ["id", "name"]}
        
        sql = compile_sql_from_intent(intent_dict, schema)
        
        # Must always return a string (even if it's a fallback SQL)
        assert isinstance(sql, str)

    @given(st.lists(st.text(min_size=1, max_size=15), min_size=0, max_size=5))
    def test_random_tables_handled(self, tables):
        """Property: Random table names should be handled without crashing."""
        schema = {"users": ["id", "name"]}
        
        intent = {
            "tables": tables,
            "columns": ["id"],
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, schema)
        assert isinstance(sql, str)

    @given(st.lists(st.text(min_size=1, max_size=15), min_size=0, max_size=10))
    def test_random_columns_handled(self, columns):
        """Property: Random column names should be handled without crashing."""
        schema = {"users": ["id", "name"]}
        
        intent = {
            "tables": ["users"],
            "columns": columns,
            "filters": []
        }
        
        sql = compile_sql_from_intent(intent, schema)
        assert isinstance(sql, str)

    @given(st.lists(st.text(min_size=1, max_size=30), min_size=0, max_size=5))
    def test_random_filters_handled(self, filters):
        """Property: Random filter expressions should be handled without crashing."""
        schema = {"users": ["id", "name"]}
        
        intent = {
            "tables": ["users"],
            "columns": ["id"],
            "filters": filters
        }
        
        sql = compile_sql_from_intent(intent, schema)
        assert isinstance(sql, str)


class TestIntentCompilerDeterminism:
    """Determinism tests for intent compiler."""

    def test_same_intent_produces_same_sql(self, sample_schema):
        """Property: Same intent should always produce identical SQL.
        
        This is critical for LLM systems to ensure reproducibility and
        prevent non-deterministic behavior.
        """
        intent = {
            "tables": ["users"],
            "columns": ["id", "name", "email"],
            "filters": ["id > 10"]
        }
        
        sql1 = compile_sql_from_intent(intent, sample_schema)
        sql2 = compile_sql_from_intent(intent, sample_schema)
        sql3 = compile_sql_from_intent(intent, sample_schema)
        
        assert sql1 == sql2 == sql3

    def test_determinism_with_empty_intent(self, sample_schema):
        """Property: Empty intent should produce consistent fallback SQL."""
        intent = {}
        
        sql1 = compile_sql_from_intent(intent, sample_schema)
        sql2 = compile_sql_from_intent(intent, sample_schema)
        
        assert sql1 == sql2

    def test_determinism_with_complex_intent(self, sample_schema):
        """Property: Complex intent should produce consistent SQL."""
        intent = {
            "tables": ["users", "orders"],
            "columns": ["id", "name", "total"],
            "filters": ["id > 5", "total < 100"]
        }
        
        sql1 = compile_sql_from_intent(intent, sample_schema)
        sql2 = compile_sql_from_intent(intent, sample_schema)
        
        assert sql1 == sql2

    @given(st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.lists(st.text(min_size=1, max_size=10), min_size=0, max_size=3),
        min_size=1, max_size=3
    ))
    def test_determinism_property(self, intent_dict):
        """Property: Determinism should hold for random intents as well."""
        schema = {"users": ["id", "name"]}
        
        sql1 = compile_sql_from_intent(intent_dict, schema)
        sql2 = compile_sql_from_intent(intent_dict, schema)
        
        assert sql1 == sql2
