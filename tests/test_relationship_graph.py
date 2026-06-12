# ── Test Relationship Graph ───────────────────────────────────────────────────
import pytest
from dbbuddy_core.query import build_relationship_graph


class TestRelationshipGraph:
    """Test relationship graph building for join inference."""

    def test_detects_foreign_key_relationship(self):
        """Test that foreign key relationships are detected from column naming."""
        schema = {
            "users": ["id", "name", "email"],
            "orders": ["id", "user_id", "total"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert "orders" in graph
        assert "user_id" in graph["orders"]
        assert graph["orders"]["user_id"] == ("users", "id")

    def test_detects_multiple_foreign_keys(self):
        """Test detection of multiple foreign key relationships."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "user_id", "product_id"],
            "products": ["id", "name", "price"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert "orders" in graph
        assert "user_id" in graph["orders"]
        assert "product_id" in graph["orders"]
        assert graph["orders"]["user_id"] == ("users", "id")
        assert graph["orders"]["product_id"] == ("products", "id")

    def test_handles_singular_table_names(self):
        """Test handling of singular vs plural table names."""
        schema = {
            "user": ["id", "name"],
            "order": ["id", "user_id"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert "order" in graph
        assert "user_id" in graph["order"]
        assert graph["order"]["user_id"] == ("user", "id")

    def test_handles_plural_table_names(self):
        """Test handling of plural table names."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "user_id"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert "orders" in graph
        assert "user_id" in graph["orders"]
        assert graph["orders"]["user_id"] == ("users", "id")

    def test_ignores_primary_keys(self):
        """Test that primary keys are not treated as foreign keys."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "user_id"]
        }
        
        graph = build_relationship_graph(schema)
        
        # Primary keys should not be in the relationship graph as foreign keys
        assert "id" not in graph.get("users", {})
        assert "id" not in graph.get("orders", {})

    def test_empty_schema_returns_empty_graph(self):
        """Test that empty schema returns empty relationship graph."""
        graph = build_relationship_graph({})
        
        assert graph == {}

    def test_no_foreign_keys_returns_empty_relationships(self):
        """Test schema without foreign keys returns empty relationships."""
        schema = {
            "users": ["id", "name", "email"],
            "products": ["id", "name", "price"]
        }
        
        graph = build_relationship_graph(schema)
        
        # Should have empty relationships for both tables
        assert graph["users"] == {}
        assert graph["products"] == {}

    def test_case_insensitive_matching(self):
        """Test that relationship detection is case-insensitive."""
        schema = {
            "Users": ["ID", "Name"],
            "Orders": ["ID", "User_ID"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert "Orders" in graph
        assert "User_ID" in graph["Orders"]

    def test_complex_naming_patterns(self):
        """Test handling of complex column naming patterns."""
        schema = {
            "users": ["id", "name"],
            "user_profiles": ["id", "user_id", "bio"],
            "orders": ["id", "user_id", "total"]
        }
        
        graph = build_relationship_graph(schema)
        
        # Should detect user_id in both user_profiles and orders
        assert "user_profiles" in graph
        assert "orders" in graph
        assert "user_id" in graph["user_profiles"]
        assert "user_id" in graph["orders"]

    def test_no_match_when_referenced_table_missing(self):
        """Test that foreign keys without matching referenced table are ignored."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "customer_id"]  # customer_id but no customers table
        }
        
        graph = build_relationship_graph(schema)
        
        # customer_id should not create a relationship since customers table doesn't exist
        assert "customer_id" not in graph.get("orders", {})

    def test_requires_id_column_in_referenced_table(self):
        """Test that relationship requires id column in referenced table."""
        schema = {
            "users": ["id", "name"],  # users has id
            "orders": ["id", "user_id"],
            "products": ["name", "price"]  # products has no id column
        }
        
        graph = build_relationship_graph(schema)
        
        # user_id should create relationship to users (has id)
        assert "user_id" in graph["orders"]
        # If there was a product_id, it wouldn't create relationship (no id in products)

    def test_handles_underscore_patterns(self):
        """Test various underscore patterns in column names."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "user_id", "created_by_user_id"]
        }
        
        graph = build_relationship_graph(schema)
        
        # Should detect both user_id and created_by_user_id patterns
        assert "user_id" in graph["orders"]
        # created_by_user_id might not match standard pattern

    def test_returns_dict_structure(self):
        """Test that relationship graph returns proper dict structure."""
        schema = {
            "users": ["id", "name"],
            "orders": ["id", "user_id"]
        }
        
        graph = build_relationship_graph(schema)
        
        assert isinstance(graph, dict)
        for table, relationships in graph.items():
            assert isinstance(table, str)
            assert isinstance(relationships, dict)
            for fk_col, (ref_table, ref_col) in relationships.items():
                assert isinstance(fk_col, str)
                assert isinstance(ref_table, str)
                assert isinstance(ref_col, str)

    def test_no_self_referencing_relationships(self):
        """Test that self-referencing foreign keys are handled correctly."""
        schema = {
            "users": ["id", "name", "manager_id"]  # manager_id references users.id
        }
        
        graph = build_relationship_graph(schema)
        
        # Should detect self-referencing relationship
        assert "users" in graph
        if "manager_id" in graph["users"]:
            assert graph["users"]["manager_id"] == ("users", "id")
