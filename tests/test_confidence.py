# ── Test Confidence Scoring ───────────────────────────────────────────────────
import pytest
from dbbuddy_core.pipeline import calculate_confidence


class TestConfidenceScoring:
    """Test confidence scoring system with multi-factor evaluation."""

    def test_high_confidence_local_success(self):
        """Test high confidence for local model with perfect execution."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "high"

    def test_high_confidence_with_valid_sql_only(self):
        """Test high confidence with just valid SQL and local model."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "high"

    def test_medium_confidence_nemotron_success(self):
        """Test medium confidence for nemotron fallback with success."""
        response = {
            "model_used": "nemotron",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "medium"

    def test_medium_confidence_auto_fixed(self):
        """Test medium confidence when auto-fix was applied."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": True,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "medium"

    def test_low_confidence_unknown_model(self):
        """Test low confidence when model is unknown."""
        response = {
            "model_used": "unknown",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_low_confidence_schema_invalid(self):
        """Test low confidence when schema validation fails."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": False},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_low_confidence_execution_error(self):
        """Test low confidence when execution error occurred."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": "syntax error near 'FROM'",
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_low_confidence_invalid_query_type(self):
        """Test low confidence for invalid query type."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "invalid"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_confidence_multiple_penalties(self):
        """Test confidence calculation with multiple penalties."""
        response = {
            "model_used": "nemotron",  # -20
            "schema_validation": {"valid": False},  # -30
            "auto_fixed": True,  # -15
            "error": "execution error",  # -40
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_confidence_edge_case_high_threshold(self):
        """Test confidence at high threshold boundary (80)."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "high"

    def test_confidence_edge_case_medium_threshold(self):
        """Test confidence at medium threshold boundary (50)."""
        response = {
            "model_used": "nemotron",
            "schema_validation": {"valid": True},
            "auto_fixed": True,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "medium"

    def test_confidence_missing_fields(self):
        """Test confidence calculation with missing optional fields."""
        response = {
            "model_used": "local",
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        # Should handle missing fields gracefully
        assert confidence in ["high", "medium", "low"]

    def test_confidence_empty_response(self):
        """Test confidence calculation with empty response."""
        response = {}
        
        confidence = calculate_confidence(response)
        assert confidence == "low"

    def test_confidence_consistency(self):
        """Test that same response always produces same confidence."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": False,
            "error": None,
            "query_type": "select"
        }
        
        confidence1 = calculate_confidence(response)
        confidence2 = calculate_confidence(response)
        
        assert confidence1 == confidence2

    def test_confidence_nemotron_with_auto_fixed(self):
        """Test confidence for nemotron with auto-fix applied."""
        response = {
            "model_used": "nemotron",
            "schema_validation": {"valid": True},
            "auto_fixed": True,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        assert confidence == "medium"

    def test_confidence_local_with_schema_invalid_and_fixed(self):
        """Test confidence when schema invalid but auto-fixed."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": False},
            "auto_fixed": True,
            "error": None,
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        # Should be low due to schema validation failure
        assert confidence == "low"

    def test_confidence_local_with_execution_error_and_fixed(self):
        """Test confidence when execution error but auto-fixed."""
        response = {
            "model_used": "local",
            "schema_validation": {"valid": True},
            "auto_fixed": True,
            "error": "original error",
            "query_type": "select"
        }
        
        confidence = calculate_confidence(response)
        # Should be low due to execution error
        assert confidence == "low"

    def test_confidence_score_boundaries(self):
        """Test confidence score boundaries."""
        # Test that scores map correctly to levels
        test_cases = [
            ({"model_used": "local", "schema_validation": {"valid": True}, "auto_fixed": False, "error": None, "query_type": "select"}, "high"),
            ({"model_used": "nemotron", "schema_validation": {"valid": True}, "auto_fixed": False, "error": None, "query_type": "select"}, "medium"),
            ({"model_used": "unknown", "schema_validation": {"valid": True}, "auto_fixed": False, "error": None, "query_type": "select"}, "low"),
        ]
        
        for response, expected in test_cases:
            confidence = calculate_confidence(response)
            assert confidence == expected
