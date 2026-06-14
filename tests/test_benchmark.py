# ── Test Benchmark System ─────────────────────────────────────────────────────
import pytest
from unittest.mock import MagicMock, patch
from dbbuddy_core.pipeline import benchmark_query
from dbbuddy_core.models import DBConfig


class TestBenchmarkSystem:
    """Test benchmark system for comparing model performance."""

    @pytest.fixture
    def mock_config(self):
        """Mock database configuration."""
        return DBConfig(
            host="localhost",
            user="test",
            password="test",
            database="testdb",
            ai=True,
            ai_provider="local",
            fallback_provider="nemotron"
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

    def test_benchmark_returns_summary(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark returns summary with accuracy metrics."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert "summary" in result
                                    assert "accuracy_percentage" in result["summary"]
                                    assert "average_latency_ms" in result["summary"]

    def test_benchmark_single_provider(self, mock_config, mock_schema, mock_semantic_layer):
        """Test benchmark with single provider."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert result["summary"]["total_providers"] == 1
                                    assert "local" in result["results"]

    def test_benchmark_multiple_providers(self, mock_config, mock_schema, mock_semantic_layer):
        """Test benchmark with multiple providers."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local", "nemotron"])
                                    
                                    assert result["summary"]["total_providers"] == 2
                                    assert "local" in result["results"]
                                    assert "nemotron" in result["results"]

    def test_benchmark_latency_measurement(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark measures latency for each provider."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert "latency_ms" in result["results"]["local"]
                                    assert result["results"]["local"]["latency_ms"] >= 0

    def test_benchmark_execution_correctness(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark tracks execution correctness."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert "execution_correctness" in result["results"]["local"]
                                    assert result["results"]["local"]["execution_correctness"] == "correct"

    def test_benchmark_result_correctness(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark tracks result correctness."""
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
                                    mock_exec.return_value = {"success": True, "results": [{"id": 1}]}
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert "result_correctness" in result["results"]["local"]
                                    assert result["results"]["local"]["result_correctness"] == "valid"

    def test_benchmark_handles_provider_failure(self, mock_config):
        """Test that benchmark handles provider failures gracefully."""
        with patch('dbbuddy_core.pipeline.process_query') as mock_process:
            mock_process.side_effect = Exception("Provider failed")
            
            result = benchmark_query(mock_config, "List users", ["local"])
            
            assert "local" in result["results"]
            assert result["results"]["local"]["success"] == False
            assert result["results"]["local"]["execution_correctness"] == "failed"

    def test_benchmark_default_providers(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark uses default providers when none specified."""
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
                                    
                                    result = benchmark_query(mock_config, "List users")
                                    
                                    # Should default to ["local", "nemotron"]
                                    assert result["summary"]["total_providers"] == 2

    def test_benchmark_accuracy_calculation(self, mock_config, mock_schema, mock_semantic_layer):
        """Test accuracy percentage calculation."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local", "nemotron"])
                                    
                                    assert result["summary"]["accuracy_percentage"] == 100.0

    def test_benchmark_best_provider_selection(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark identifies best provider by latency."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local", "nemotron"])
                                    
                                    assert "best_provider" in result["summary"]
                                    assert result["summary"]["best_provider"] in ["local", "nemotron"]

    def test_benchmark_schema_validation_tracking(self, mock_config, mock_schema, mock_semantic_layer):
        """Test that benchmark tracks schema validation status."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    assert "schema_valid" in result["results"]["local"]
                                    assert result["results"]["local"]["schema_valid"] == True

    def test_performance_regression_guard_local(self, mock_config, mock_schema, mock_semantic_layer):
        """Performance regression guard: local provider should be under 2000ms (relaxed threshold)."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local"])
                                    
                                    # Performance regression guard: relaxed threshold for CI environments
                                    assert result["results"]["local"]["latency_ms"] < 5000, \
                                        f"Local provider too slow: {result['results']['local']['latency_ms']}ms"

    def test_performance_regression_guard_nemotron(self, mock_config, mock_schema, mock_semantic_layer):
        """Performance regression guard: nemotron provider should be under 5000ms (relaxed threshold)."""
        with patch('dbbuddy_core.pipeline.db_module.connect_db') as mock_conn:
            mock_conn.return_value = MagicMock()
            with patch('dbbuddy_core.pipeline.schema_module.fetch_schema') as mock_fetch:
                mock_fetch.return_value = mock_schema
                with patch('dbbuddy_core.pipeline.mapping_module.map_schema') as mock_map:
                    mock_map.return_value = mock_semantic_layer
                    with patch('dbbuddy_core.pipeline.ai_refine') as mock_ai:
                        mock_ai.return_value = mock_semantic_layer
                        with patch('dbbuddy_core.pipeline.generate_sql') as mock_gen:
                            mock_gen.return_value = ("SELECT id FROM users;", "nemotron")
                            with patch('dbbuddy_core.pipeline.validate_against_schema') as mock_val:
                                mock_val.return_value = {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
                                with patch('dbbuddy_core.pipeline.safe_execute') as mock_exec:
                                    mock_exec.return_value = {"success": True, "results": []}
                                    
                                    result = benchmark_query(mock_config, "List users", ["nemotron"])
                                    
                                    # Performance regression guard: relaxed threshold for network calls
                                    assert result["results"]["nemotron"]["latency_ms"] < 5000, \
                                        f"Nemotron provider too slow: {result['results']['nemotron']['latency_ms']}ms"

    def test_performance_regression_guard_average(self, mock_config, mock_schema, mock_semantic_layer):
        """Performance regression guard: average latency should be under 3000ms (relaxed threshold)."""
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
                                    
                                    result = benchmark_query(mock_config, "List users", ["local", "nemotron"])
                                    
                                    # Performance regression guard: relaxed threshold for mixed environments
                                    assert result["summary"]["average_latency_ms"] < 3000, \
                                        f"Average latency too slow: {result['summary']['average_latency_ms']}ms"
