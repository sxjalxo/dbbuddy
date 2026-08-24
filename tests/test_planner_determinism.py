"""Table/measure resolution must be deterministic across processes.

A column name that lives on more than one table (``unit_price`` on both
``products`` and ``order_items``) used to be resolved through
``list(set(tables))``, whose iteration order follows the hash seed and therefore
differs from one process to the next. The observable symptom was an ERP-grade
defect: the *same* question compiled to ``MAX(products.unit_price)`` on one run
and ``MAX(order_items.unit_price)`` on the next — a different answer to an
identical query. These tests pin the order-preserving behaviour that replaced it.
"""

from dbbuddy_core.intent_builder import detect_tables_from_columns


def test_detect_tables_preserves_first_seen_order():
    columns = [
        {"table": "products", "column": "unit_price"},
        {"table": "order_items", "column": "unit_price"},
        {"table": "products", "column": "name"},
    ]
    # First occurrence wins, later duplicates dropped — never hash order.
    assert detect_tables_from_columns(columns) == ["products", "order_items"]


def test_detect_tables_is_stable_regardless_of_value_hashing():
    # Table names chosen so a set would very likely reorder them; the function
    # must still return them in the order the columns present them.
    columns = [{"table": t, "column": "x"} for t in
               ("zeta", "alpha", "mike", "bravo", "yankee")]
    expected = ["zeta", "alpha", "mike", "bravo", "yankee"]
    for _ in range(5):
        assert detect_tables_from_columns(columns) == expected
