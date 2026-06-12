# ── Test Query Pipeline ─────────────────────────────────────────────────────
import pytest
from hypothesis import given, strategies as st
from unittest.mock import MagicMock, patch
from dbbuddy_core.pipeline import process_query, calculate_confidence
from dbbuddy_core.models import DBConfig


class TestQueryPipeline:
    """Test core query processing pipeline with new systems."""

    @pytest.fixture
    def mock_schema(self):
        """Mock database schema for testing."""
        return {
            "users": ["id", "name", "email", "created_at"],
            "orders": ["id", "user_id", "total", "created_at"]
        }

    @pytest.fixture
    def mock_semantic_layer(self):
        """Mock semantic layer for testing."""
        return {
            "users": {
                "id": {"term": "identifier"},
                "name": {"term": "name"},
                "email": {"term": "email"}
            },
            "orders": {
                "id": {"term": "identifier"},
                "user_id": {"term": "user identifier"}
            }
        }

    @pytest.fixture
    def mock_config(self):
        """Mock database configuration."""
        return DBConfig(
            host="localhost",
            user="test",
            password="test",
            database="testdb",
            ai=True,
            ai_provider="local"
        )

    def test_simple_select_query_structure(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that simple select queries produce valid SQL structure."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id, name FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"id": 1, "name": "test"}]}
                                    
                                    result = process_query(mock_config, "List all users")
                                    
                                    assert result["sql"].startswith("SELECT")
                                    assert "users" in result["sql"].lower()
                                    assert result["query_type"] == "select"
                                    assert result["model_used"] in ["local", "nemotron", "unknown"]

    def test_semantic_interpretation_included(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that semantic interpretation is included in response."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id, email FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": []}
                                    
                                    result = process_query(mock_config, "List users with email")
                                    
                                    assert "semantic_interpretation" in result
                                    assert isinstance(result["semantic_interpretation"], dict)

    def test_intent_explanation_included(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that intent explanation is included in response."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": []}
                                    
                                    result = process_query(mock_config, "List users")
                                    
                                    assert "intent_explanation" in result
                                    assert isinstance(result["intent_explanation"], dict)

    def test_confidence_calculation_high(self):
        """Test confidence calculation for high-confidence scenario."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "high"

    def test_confidence_calculation_medium(self):
        """Test confidence calculation for medium-confidence scenario."""
        response = {
            "model_used": "nemotron",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "medium"

    def test_confidence_calculation_low(self):
        """Test confidence calculation for low-confidence scenario."""
        response = {
            "model_used": "unknown",
            "schema_validation": {"valid": False},
            "auto_fixed": True,
            "error": "syntax error",
            "query_type": "invalid"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_invalid_query_handling(self, mock_config, mock_schema, mock_semantic_layer):
        """Test handling of invalid queries."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("unknown", "unknown")
                            
                            result = process_query(mock_config, "invalid query")
                            
                            assert result["query_type"] == "invalid"
                            assert result["confidence"] == "low"
                            assert result["auto_executed"] == False

    def test_schema_validation_failure_handling(self, mock_config, mock_schema, mock_semantic_layer):
        """Test handling of schema validation failures."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id FROM unknown_table;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {
                                    "valid": False,
                                    "unknown_tables": ["unknown_table"],
                                    "unknown_columns": [],
                                    "invalid_joins": []
                                }
                                with patch('dbbuddy_core.pipeline.fix_sql') as mock_fix:
                                    mock_fix.return_value = ("SELECT id FROM users;", "local")
                                    with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val2:
                                        mock_val2.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                        with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                            mock_exec.return_value = {"success": True, "results": []}
                                            
                                            result = process_query(mock_config, "List from unknown")
                                            
                                            assert result["auto_fixed"] == True
                                            assert "unknown_table" in result.get("original_error", "") or result.get("warning", "")

    def test_end_to_end_query_pipeline(self, mock_config, mock_schema, mock_semantic_layer):
        """End-to-end test: validates complete pipeline from NL to SQL to execution.
        
        This is the crown jewel test that validates:
        - semantic layer
        - intent
        - LLM / fallback
        - validation
        - execution
        """
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id, email FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"id": 1, "email": "test@example.com"}]}
                                    
                                    # Execute end-to-end query
                                    result = process_query(mock_config, "List all users with email")
                                    
                                    # Validate semantic layer was used
                                    assert result["semantic_layer"] == mock_semantic_layer
                                    
                                    # Validate SQL generation
                                    assert result["sql"].lower().startswith("select")
                                    assert "users" in result["sql"].lower()
                                    assert "email" in result["sql"].lower()
                                    
                                    # Validate intent explanation was generated
                                    assert "intent_explanation" in result
                                    assert isinstance(result["intent_explanation"], dict)
                                    
                                    # Validate model tracking (capability-based: some model was used)
                                    assert result["model_used"] in ["local", "nemotron", "unknown"]
                                    
                                    # Validate schema validation passed
                                    assert result["schema_validation"]["valid"] == True
                                    
                                    # Validate execution succeeded
                                    assert result["auto_executed"] == True
                                    assert result["results"] == [{"id": 1, "email": "test@example.com"}]
                                    
                                    # Validate confidence scoring
                                    assert result["confidence"] == "high"
                                    
                                    # Validate semantic interpretation
                                    assert "semantic_interpretation" in result
                                    
                                    # Validate query type detection
                                    assert result["query_type"] == "select"


class TestPipelineChaos:
    """Chaos testing for full pipeline resilience - final boss test."""

    @pytest.fixture
    def mock_config(self):
        """Mock database configuration."""
        return DBConfig(
            host="localhost",
            user="test",
            password="test",
            database="testdb",
            ai=True,
            ai_provider="local"
        )

    @pytest.fixture
    def mock_schema(self):
        """Mock database schema."""
        return {
            "users": ["id", "name", "email"],
            "orders": ["id", "user_id", "total"]
        }

    @pytest.fixture
    def mock_semantic_layer(self):
        """Mock semantic layer."""
        return {
            "users": {
                "id": {"term": "identifier"},
                "name": {"term": "name"}
            }
        }

    @given(st.text(min_size=1, max_size=200))
    def test_full_pipeline_never_crashes(self, mock_config, mock_schema, mock_semantic_layer, query):
        """Property: Full pipeline should never crash on any input.
        
        This is the final boss test - combines all systems and tests
        real-world resilience against chaotic input.
        """
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": []}
                                    
                                    try:
                                        result = process_query(mock_config, query)
                                        # Should always return a dict
                                        assert isinstance(result, dict)
                                        # Should have required fields
                                        assert "sql" in result
                                        assert "query_type" in result
                                        assert "confidence" in result
                                    except Exception as e:
                                        # Should never crash on any input
                                        pytest.fail(f"Pipeline crashed on input: {query}. Error: {e}")

    @given(st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.lists(st.text(min_size=1, max_size=10), min_size=0, max_size=5),
        min_size=0, max_size=3
    ))
    def test_confidence_calculation_never_crashes(self, response_dict):
        """Property: Confidence calculation should never crash on any response."""
        try:
            confidence = calculate_confidence(response_dict)
            # Should always return a valid confidence level
            assert confidence in ["high", "medium", "low"]
        except Exception as e:
            pytest.fail(f"Confidence calculation crashed. Error: {e}")


class TestQuerySafety:
    """Test query safety classification and confirmation behavior."""

    def test_read_query_auto_executes(self, mock_config, mock_schema, mock_semantic_layer):
        """READ queries should auto-execute without confirmation."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT * FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"id": 1, "name": "test"}]}
                                    
                                    result = process_query(mock_config, "List all users")
                                    
                                    assert result["requires_confirmation"] is False
                                    assert result["auto_executed"] is True
                                    assert result["safety_category"] == "read"
                                    assert "results" in result

    def test_write_query_requires_confirmation(self, mock_config, mock_schema, mock_semantic_layer):
        """WRITE queries should require user confirmation."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("DELETE FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                
                                result = process_query(mock_config, "Delete all users")
                                
                                assert result["requires_confirmation"] is True
                                assert result["auto_executed"] is False
                                assert result["safety_category"] == "write"
                                assert "warning" in result
                                assert "DELETE" in result["warning"]

    def test_drop_query_generates_strong_warning(self, mock_config, mock_schema, mock_semantic_layer):
        """DROP queries should generate strong warnings."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("DROP TABLE users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                
                                result = process_query(mock_config, "Drop users table")
                                
                                assert result["requires_confirmation"] is True
                                assert "🚨" in result["warning"]
                                assert "permanently lost" in result["warning"]

    def test_multi_statement_treated_as_write(self):
        """Multi-statement queries should be treated as write operations."""
        from dbbuddy_core.pipeline import classify_query_safety
        
        sql = "SELECT * FROM users; DELETE FROM orders;"
        category, requires_confirmation = classify_query_safety(sql)
        
        assert category == "write"
        assert requires_confirmation is True

    def test_cte_with_delete_treated_as_write(self):
        """CTE with DELETE should be treated as write operation."""
        from dbbuddy_core.pipeline import classify_query_safety
        
        sql = "WITH temp AS (SELECT * FROM users) DELETE FROM temp;"
        category, requires_confirmation = classify_query_safety(sql)
        
        assert category == "write"
        assert requires_confirmation is True

    def test_injection_attempt_treated_as_write(self):
        """Sneaky injection attempts should be treated as write operations."""
        from dbbuddy_core.pipeline import classify_query_safety
        
        sql = "SELECT * FROM users; DROP TABLE users;"
        category, requires_confirmation = classify_query_safety(sql)
        
        assert category == "write"
        assert requires_confirmation is True

    def test_show_query_auto_executes(self):
        """SHOW queries should auto-execute as read operations."""
        from dbbuddy_core.pipeline import classify_query_safety
        
        sql = "SHOW TABLES;"
        category, requires_confirmation = classify_query_safety(sql)
        
        assert category == "read"
        assert requires_confirmation is False

    def test_insert_requires_confirmation(self, mock_config, mock_schema, mock_semantic_layer):
        """INSERT queries should require confirmation."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("INSERT INTO users (name) VALUES ('test');", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                
                                result = process_query(mock_config, "Add a new user")
                                
                                assert result["requires_confirmation"] is True
                                assert result["auto_executed"] is False
                                assert "INSERT" in result["warning"]

    def test_delete_with_dry_run_estimate(self, mock_config, mock_schema, mock_semantic_layer):
        """DELETE queries should include dry run estimate."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("DELETE FROM users WHERE id = 5;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"COUNT(*)": 1}]}
                                
                                result = process_query(mock_config, "Delete user with id 5")
                                
                                assert result["requires_confirmation"] is True
                                assert "dry_run" in result
                                assert result["dry_run"]["estimated_rows"] == 1
                                assert "affect 1 row" in result["warning"]

    def test_update_with_dry_run_estimate(self, mock_config, mock_schema, mock_semantic_layer):
        """UPDATE queries should include dry run estimate."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("UPDATE users SET name = 'test' WHERE id = 10;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"COUNT(*)": 5}]}
                                
                                result = process_query(mock_config, "Update user name")
                                
                                assert result["requires_confirmation"] is True
                                assert "dry_run" in result
                                assert result["dry_run"]["estimated_rows"] == 5
                                assert "affect 5 rows" in result["warning"]

    def test_delete_all_rows_estimate(self, mock_config, mock_schema, mock_semantic_layer):
        """DELETE without WHERE should estimate all rows."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("DELETE FROM users;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"COUNT(*)": 100}]}
                                
                                result = process_query(mock_config, "Delete all users")
                                
                                assert result["requires_confirmation"] is True
                                assert "dry_run" in result
                                assert result["dry_run"]["estimated_rows"] == 100
                                assert "affect 100 rows" in result["warning"]

    def test_zero_rows_estimate(self, mock_config, mock_schema, mock_semantic_layer):
        """DELETE with no matching rows should show 0 estimate."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("DELETE FROM users WHERE id = 999;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"COUNT(*)": 0}]}
                                
                                result = process_query(mock_config, "Delete non-existent user")
                                
                                assert result["requires_confirmation"] is True
                                assert "dry_run" in result
                                assert result["dry_run"]["estimated_rows"] == 0
                                assert "affect 0 rows" in result["warning"]
                                assert "no data will be changed" in result["warning"]

    def test_generate_count_query_delete(self):
        """Test count query generation for DELETE statements."""
        from dbbuddy_core.pipeline import generate_count_query
        
        # DELETE with WHERE
        sql = "DELETE FROM users WHERE id = 5"
        count = generate_count_query(sql)
        assert count == "SELECT COUNT(*) FROM users WHERE id = 5"
        
        # DELETE without WHERE
        sql = "DELETE FROM users"
        count = generate_count_query(sql)
        assert count == "SELECT COUNT(*) FROM users"
        
        # Not a DELETE
        sql = "SELECT * FROM users"
        count = generate_count_query(sql)
        assert count is None

    def test_generate_count_query_update(self):
        """Test count query generation for UPDATE statements."""
        from dbbuddy_core.pipeline import generate_count_query
        
        # UPDATE with WHERE
        sql = "UPDATE users SET name = 'test' WHERE id = 10"
        count = generate_count_query(sql)
        assert count == "SELECT COUNT(*) FROM users WHERE id = 10"
        
        # UPDATE without WHERE
        sql = "UPDATE users SET name = 'test'"
        count = generate_count_query(sql)
        assert count == "SELECT COUNT(*) FROM users"
        
        # Not an UPDATE
        sql = "INSERT INTO users (name) VALUES ('test')"
        count = generate_count_query(sql)
        assert count is None

    def test_validate_aggregation_valid(self):
        """Test aggregation validation with valid GROUP BY."""
        from dbbuddy_core.pipeline import validate_aggregation
        
        # Valid: all non-aggregated columns in GROUP BY
        sql = "SELECT u.name, SUM(o.amount) FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id, u.name"
        result = validate_aggregation(sql)
        assert result["valid"] is True
        assert result["error"] is None
        assert len(result["violations"]) == 0

    def test_validate_aggregation_invalid(self):
        """Test aggregation validation with GROUP BY violation."""
        from dbbuddy_core.pipeline import validate_aggregation
        
        # Invalid: o.status not in GROUP BY and not aggregated
        sql = "SELECT u.name, SUM(o.amount), o.status FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id"
        result = validate_aggregation(sql)
        assert result["valid"] is False
        assert result["error"] is not None
        assert "o.status" in result["error"]
        assert len(result["violations"]) > 0

    def test_validate_aggregation_no_group_by(self):
        """Test aggregation validation with no GROUP BY clause."""
        from dbbuddy_core.pipeline import validate_aggregation
        
        # No GROUP BY - should pass
        sql = "SELECT * FROM users"
        result = validate_aggregation(sql)
        assert result["valid"] is True

    def test_validate_aggregation_all_aggregated(self):
        """Test aggregation validation when all columns are aggregated."""
        from dbbuddy_core.pipeline import validate_aggregation
        
        # All columns aggregated - should pass
        sql = "SELECT SUM(amount), COUNT(*) FROM orders GROUP BY user_id"
        result = validate_aggregation(sql)
        assert result["valid"] is True

    def test_aggregation_validation_in_pipeline(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that aggregation validation is integrated into pipeline."""
        from dbbuddy_core.pipeline import process_query
        from unittest.mock import patch, MagicMock
        
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            # Generate SQL with GROUP BY violation AND joins
                            mock_gen.return_value = ("SELECT u.name, SUM(o.amount), o.status FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                
                                result = process_query(mock_config, "Top customers by spending")
                                
                                # Should fail aggregation validation
                                assert result["auto_executed"] is False
                                assert "aggregation_validation" in result
                                assert result["aggregation_validation"]["valid"] is False
                                assert result["warning"] is not None
                                assert "GROUP BY violation" in result["warning"]

    def test_is_query_relevant_valid(self):
        """Test relevance detection with database-related queries using weighted scoring."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "email": {"term": "email"}
            },
            "orders": {
                "total_amount": {"term": "revenue"},
                "status": {"term": "status"}
            }
        }
        
        # Valid database queries with table matches (strong signal)
        result = is_query_relevant("Show total revenue", semantic)
        assert result["relevant"] is True
        assert "revenue" in result["matched_terms"]
        assert result["score"] >= 1.5  # Should pass threshold
        
        result = is_query_relevant("List all users", semantic)
        assert result["relevant"] is True
        assert "users" in result["matched_terms"]
        assert result["score"] >= 2.0  # Table match = 2.0 points

    def test_is_query_relevant_invalid(self):
        """Test relevance detection with non-database queries using weighted scoring."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "email": {"term": "email"}
            },
            "orders": {
                "total_amount": {"term": "revenue"},
                "status": {"term": "status"}
            }
        }
        
        # Invalid non-database queries
        result = is_query_relevant("How are you?", semantic)
        assert result["relevant"] is False
        assert len(result["matched_terms"]) == 0
        assert result["score"] == 0
        
        result = is_query_relevant("Tell me a joke", semantic)
        assert result["relevant"] is False
        assert result["score"] == 0
        
        result = is_query_relevant("What is AI?", semantic)
        assert result["relevant"] is False
        assert result["score"] == 0

    def test_weighted_scoring_table_vs_column(self):
        """Test that table matches have higher weight than column matches."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "id": {"term": "id"}
            }
        }
        
        # Table match should score higher than column match
        table_result = is_query_relevant("users", semantic)
        column_result = is_query_relevant("id", semantic)
        
        assert table_result["score"] > column_result["score"]
        assert table_result["score"] == 2.0  # Table match = 2.0
        assert column_result["score"] == 1.5  # Column match = 1.5

    def test_coverage_awareness_edge_case(self):
        """Test that queries with low coverage are rejected even with strong matches (but no table match)."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "id": {"term": "id"}
            }
        }
        
        # Edge case: one strong match but low coverage (no table match)
        # "id something random blah blah" - has "id" but most tokens are irrelevant
        result = is_query_relevant("id something random blah blah", semantic)
        
        # Should have some score but low coverage (< 20%)
        assert result["score"] >= 1.5  # Column match = 1.5
        assert result["coverage"] < 0.2  # Low coverage (1 match / 6 tokens = 16.7%)
        assert result["relevant"] is False  # Should be rejected due to low coverage and no table match

    def test_table_match_bypasses_coverage(self):
        """Test that table matches bypass coverage requirement."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "email": {"term": "email"}
            }
        }
        
        # Query with table match but low coverage
        # "List all users with their emails" - has "users" table match, coverage = 1/6 = 16.7%
        result = is_query_relevant("List all users with their emails", semantic)
        
        # Should be accepted due to table match despite low coverage
        assert result["score"] >= 2.0  # Table match = 2.0
        assert result["coverage"] < 0.2  # Low coverage
        assert result["relevant"] is True  # Should be accepted due to table match

    def test_semantic_match_short_query(self):
        """Test that semantic matches in short queries are accepted (intent queries)."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "orders": {
                "total_amount": {"term": "revenue"},
                "status": {"term": "status"}
            }
        }
        
        # Short query with semantic term only
        # "Show total revenue" - has "revenue" semantic match, no table/column match
        result = is_query_relevant("Show total revenue", semantic)
        
        # Should be accepted due to semantic match in short query (<=5 tokens)
        assert result["score"] >= 1.0  # Semantic match = 1.0
        assert len(result["semantic_matches"]) >= 1  # Has semantic match
        assert result["relevant"] is True  # Should be accepted due to semantic match

    def test_semantic_match_long_query_rejected(self):
        """Test that semantic matches in long queries without table/column are rejected."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "orders": {
                "total_amount": {"term": "revenue"},
                "status": {"term": "status"}
            }
        }
        
        # Long query with semantic term only (no table/column)
        # "Show me the total revenue for the last quarter please" - has "revenue" but long
        result = is_query_relevant("Show me the total revenue for the last quarter please", semantic)
        
        # Should be rejected due to long query with only semantic match
        assert len(result["semantic_matches"]) >= 1  # Has semantic match
        assert result["relevant"] is False  # Should be rejected (long query, no table/column)

    def test_relevance_no_crash(self):
        """Test that relevance detection doesn't crash on basic queries."""
        from dbbuddy_core.pipeline import is_query_relevant
        
        semantic = {
            "users": {
                "name": {"term": "name"},
                "email": {"term": "email"}
            }
        }
        
        # Basic query that should not crash
        result = is_query_relevant("List all users with their emails", semantic)
        
        # Should have all required fields
        assert "coverage" in result
        assert "score" in result
        assert "relevant" in result
        assert "matched_terms" in result

    def test_generate_term_interpretation(self):
        """Test term interpretation generation."""
        from dbbuddy_core.pipeline import generate_term_interpretation
        
        semantic = {
            "orders": {
                "total_amount": {"term": "revenue"},
                "status": {"term": "status"}
            }
        }
        
        relevance_check = {
            "matched_terms": ["revenue", "orders"],
            "significant_matches": ["revenue", "orders"]
        }
        
        interpretations = generate_term_interpretation("Show total revenue", semantic, relevance_check)
        
        # Should interpret "revenue" as "orders.total_amount"
        assert len(interpretations) > 0
        revenue_interp = [i for i in interpretations if i["term"] == "revenue"]
        assert len(revenue_interp) > 0
        assert revenue_interp[0]["mapped_to"] == "orders.total_amount"
        assert revenue_interp[0]["type"] == "semantic_mapping"

    def test_relevance_detection_in_pipeline(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that irrelevant queries are filtered out in pipeline."""
        from dbbuddy_core.pipeline import process_query
        from unittest.mock import patch, MagicMock
        
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        
                        # Test with irrelevant query
                        result = process_query(mock_config, "How are you?")
                        
                        # Should be rejected as irrelevant
                        assert result["auto_executed"] is False
                        assert result["sql"] is None
                        assert result["query_type"] == "invalid"
                        assert "relevance_check" in result
                        assert result["relevance_check"]["relevant"] is False
                        assert "error" in result
                        assert "doesn't appear to be a database query" in result["error"]

    def test_extract_affected_columns_single(self):
        """Test extracting single affected column from UPDATE."""
        from dbbuddy_core.pipeline import extract_affected_columns
        
        sql = "UPDATE users SET name = 'test' WHERE id = 10"
        columns = extract_affected_columns(sql)
        assert columns == ["name"]

    def test_extract_affected_columns_multiple(self):
        """Test extracting multiple affected columns from UPDATE."""
        from dbbuddy_core.pipeline import extract_affected_columns
        
        sql = "UPDATE users SET name = 'test', email = 'test@example.com', age = 25 WHERE id = 10"
        columns = extract_affected_columns(sql)
        assert set(columns) == {"name", "email", "age"}

    def test_extract_affected_columns_with_function(self):
        """Test extracting columns when functions are used."""
        from dbbuddy_core.pipeline import extract_affected_columns
        
        sql = "UPDATE users SET name = UPPER(name), updated_at = NOW() WHERE id = 10"
        columns = extract_affected_columns(sql)
        assert set(columns) == {"name", "updated_at"}

    def test_extract_affected_columns_not_update(self):
        """Test that non-UPDATE queries return empty list."""
        from dbbuddy_core.pipeline import extract_affected_columns
        
        sql = "DELETE FROM users WHERE id = 10"
        columns = extract_affected_columns(sql)
        assert columns == []

    def test_update_with_affected_columns_in_warning(self, mock_config, mock_schema, mock_semantic_layer):
        """UPDATE queries should show affected columns in warning."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("UPDATE users SET name = 'test', email = 'test@example.com' WHERE id = 10;", "local")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": [{"COUNT(*)": 1}]}
                                
                                result = process_query(mock_config, "Update user name and email")
                                
                                assert result["requires_confirmation"] is True
                                assert "dry_run" in result
                                assert result["dry_run"]["estimated_rows"] == 1
                                assert "affected_columns" in result["dry_run"]
                                assert set(result["dry_run"]["affected_columns"]) == {"name", "email"}
                                assert "Affected columns" in result["warning"]
                                assert "name" in result["warning"]
                                assert "email" in result["warning"]
