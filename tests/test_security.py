# ── Test Security & Adversarial Inputs ───────────────────────────────────────
from dbbuddy_core.query import validate_against_schema, _extract_identifiers


class TestSecurityAdversarialInputs:
    """Test security against adversarial and malicious inputs."""

    def test_schema_validation_with_malicious_tables(self):
        """Test schema validation with malicious table references."""
        schema = {"users": ["id", "name"], "orders": ["id", "total"]}

        malicious_queries = [
            "SELECT * FROM users; DROP TABLE orders;",
            "SELECT * FROM users UNION SELECT * FROM admin",
            "SELECT * FROM users WHERE 1=1; DELETE FROM orders"
        ]

        for query in malicious_queries:
            result = validate_against_schema(query, schema)
            # Should handle without crashing
            assert "valid" in result

    def test_identifier_extraction_with_attacks(self):
        """Test identifier extraction with attack patterns."""
        attack_queries = [
            "SELECT * FROM users; DROP TABLE orders;",
            "SELECT * FROM users WHERE id = 1; -- DELETE FROM users",
            "SELECT * FROM users WHERE id = 1 UNION SELECT * FROM admin"
        ]

        for query in attack_queries:
            tables, columns, joins = _extract_identifiers(query)
            # Should extract without crashing
            assert isinstance(tables, list)
            assert isinstance(columns, list)
            assert isinstance(joins, list)

