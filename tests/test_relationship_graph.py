"""Join-graph inference.

Targets ``dbbuddy_core.relationship_graph.build_relationship_graph`` — the one the
planner actually uses. These tests used to import a *second*, name-heuristic-only
copy from ``dbbuddy_core.query`` that nothing in production called, so the suite
was validating dead code while the real builder went untested. Both duplicates
(``query.build_relationship_graph``, ``intent_builder.build_relationship_graph``)
are deleted; their behavior is folded in here.

Output shape: ``{table: {neighbor_table: (from_col, to_col)}}`` — keyed by the
*neighbor*, not by the FK column, because the planner asks "how do I get from A to
B", not "what does this column point at".
"""

from dbbuddy_core.relationship_graph import build_relationship_graph


class TestDeclaredForeignKeys:
    """When the database declares FKs they win — no name guessing at all."""

    def test_declared_fks_beat_naming(self):
        # Odoo-style: the FK column name does not contain the table name.
        schema = {"sale_order": ["id", "partner_id"], "res_partner": ["id", "name"]}
        fks = {"sale_order": [("partner_id", "res_partner", "id")]}

        graph = build_relationship_graph(schema, fks)

        assert graph["sale_order"]["res_partner"] == ("partner_id", "id")
        assert graph["res_partner"]["sale_order"] == ("id", "partner_id")

    def test_cryptic_keys_resolve(self):
        # SAP: no "_id" anywhere; only the catalog knows.
        schema = {"VBAK": ["VBELN", "KUNNR"], "KNA1": ["KUNNR", "NAME1"]}
        fks = {"VBAK": [("KUNNR", "KNA1", "KUNNR")]}

        graph = build_relationship_graph(schema, fks)

        assert graph["VBAK"]["KNA1"] == ("KUNNR", "KUNNR")

    def test_two_fks_to_the_same_table_keep_the_first(self):
        schema = {"transfers": ["id", "from_account_id", "to_account_id"],
                  "accounts": ["id", "balance"]}
        fks = {"transfers": [("from_account_id", "accounts", "id"),
                             ("to_account_id", "accounts", "id")]}

        graph = build_relationship_graph(schema, fks)

        # One edge, and it is a real column rather than a guess.
        assert graph["transfers"]["accounts"] == ("from_account_id", "id")

    def test_fks_to_tables_outside_the_schema_are_ignored(self):
        schema = {"orders": ["id", "user_id"]}
        fks = {"orders": [("user_id", "users", "id")]}  # "users" not in schema

        graph = build_relationship_graph(schema, fks)

        assert graph["orders"] == {}

    def test_declared_fks_suppress_the_heuristic_entirely(self):
        # A schema that declares *some* FKs must not get name-guessed extras: the
        # catalog is authoritative, and mixing sources invents joins.
        schema = {"orders": ["id", "user_id", "product_id"],
                  "users": ["id"], "products": ["id"]}
        fks = {"orders": [("user_id", "users", "id")]}

        graph = build_relationship_graph(schema, fks)

        assert "users" in graph["orders"]
        assert "products" not in graph["orders"]


class TestNamingHeuristic:
    """Fallback for databases that declare no FKs at all (common in MySQL/MyISAM)."""

    def test_detects_plural_target(self):
        schema = {"users": ["id", "name"], "orders": ["id", "user_id", "total"]}
        graph = build_relationship_graph(schema)
        assert graph["orders"]["users"] == ("user_id", "id")
        assert graph["users"]["orders"] == ("id", "user_id")

    def test_detects_singular_target(self):
        # Singular table names are as common as plural; the old builder in
        # relationship_graph.py only tried "<x>s" and silently lost these joins.
        schema = {"user": ["id", "name"], "order": ["id", "user_id"]}
        graph = build_relationship_graph(schema)
        assert graph["order"]["user"] == ("user_id", "id")

    def test_detects_y_to_ies_target(self):
        schema = {"categories": ["id", "name"], "products": ["id", "category_id"]}
        graph = build_relationship_graph(schema)
        assert graph["products"]["categories"] == ("category_id", "id")

    def test_detects_es_plural_target(self):
        schema = {"addresses": ["id", "city"], "customers": ["id", "address_id"]}
        graph = build_relationship_graph(schema)
        assert graph["customers"]["addresses"] == ("address_id", "id")

    def test_detects_plural_stem_against_singular_table(self):
        schema = {"person": ["id", "name"], "visit": ["id", "persons_id"]}
        graph = build_relationship_graph(schema)
        assert graph["visit"]["person"] == ("persons_id", "id")

    def test_multiple_foreign_keys(self):
        schema = {"users": ["id", "name"], "products": ["id", "name", "price"],
                  "orders": ["id", "user_id", "product_id"]}
        graph = build_relationship_graph(schema)
        assert graph["orders"]["users"] == ("user_id", "id")
        assert graph["orders"]["products"] == ("product_id", "id")

    def test_a_tables_own_key_is_not_a_self_edge(self):
        schema = {"shipments": ["shipment_id", "carrier"]}
        graph = build_relationship_graph(schema)
        assert graph["shipments"] == {}

    def test_matching_is_case_insensitive(self):
        schema = {"Users": ["Id", "Name"], "Orders": ["Id", "User_Id"]}
        graph = build_relationship_graph(schema)
        assert graph["Orders"]["Users"] == ("User_Id", "Id")

    def test_unmatched_fk_column_produces_no_edge(self):
        schema = {"orders": ["id", "warehouse_id"], "users": ["id"]}
        graph = build_relationship_graph(schema)
        assert graph["orders"] == {}

    def test_bare_id_is_not_a_foreign_key(self):
        schema = {"users": ["id"], "orders": ["id"]}
        graph = build_relationship_graph(schema)
        assert graph["orders"] == {} and graph["users"] == {}

    def test_empty_schema(self):
        assert build_relationship_graph({}) == {}

    def test_every_table_appears_even_with_no_edges(self):
        # The planner indexes into the graph by table name; a missing key is a
        # KeyError on an isolated table rather than "no path".
        schema = {"a": ["id"], "b": ["id"], "orders": ["id", "a_id"]}
        graph = build_relationship_graph(schema)
        assert set(graph) == {"a", "b", "orders"}
        assert graph["b"] == {}
