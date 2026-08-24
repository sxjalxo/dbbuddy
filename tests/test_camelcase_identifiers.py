"""Naming-convention handling, added after dogfooding Microsoft's AdventureWorks.

AdventureWorks names every key ``XxxID`` (camelCase, no underscore) and every
multi-word table in PascalCase (``SalesOrderHeader``). Those conventions defeated
the engine's ``_id`` / underscore-only word matching: keys read as measures
(``SUM(SalesOrderID)`` for "total sales"), measures could not be matched to the
query, and multi-word tables bound to a shorter prefix table. These pin the
shared helpers that fixed it.
"""

from dbbuddy_core.intent_builder import split_identifier
from dbbuddy_core.semantic_roles import is_identifier_name


class TestIsIdentifierName:
    def test_snake_case_and_literals(self):
        for name in ("id", "pk", "uuid", "guid", "user_id", "customer_id"):
            assert is_identifier_name(name), name

    def test_camelcase_id_keys(self):
        for name in ("CustomerID", "SalesOrderID", "ProductID", "BusinessEntityID"):
            assert is_identifier_name(name), name

    def test_all_caps_words_are_not_ids(self):
        # The ID must be a camelCase boundary (lowercase char before "ID"), so
        # ordinary all-caps words that happen to end in the letters are not keys.
        for name in ("GRID", "PAID", "RFID", "VOID"):
            assert not is_identifier_name(name), name

    def test_measures_are_not_ids(self):
        for name in ("ListPrice", "StandardCost", "Freight", "salary", "revenue"):
            assert not is_identifier_name(name), name


class TestSplitIdentifier:
    def test_camelcase(self):
        assert split_identifier("ListPrice") == ["list", "price"]
        assert split_identifier("SalesOrderID") == ["sales", "order", "id"]
        assert split_identifier("TaxAmt") == ["tax", "amt"]

    def test_snake_case(self):
        assert split_identifier("unit_price") == ["unit", "price"]

    def test_digits_split_out(self):
        assert split_identifier("Address2024") == ["address", "2024"]

    def test_single_word(self):
        assert split_identifier("Freight") == ["freight"]
        assert split_identifier("color") == ["color"]
