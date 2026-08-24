"""Phase D — subquery predicates: EXISTS / NOT EXISTS (D1) and ANY / ALL (D2).

Covers the Predicate AST (Condition / ExistsPredicate / QuantifiedPredicate), the
build_predicate dispatcher, correlated-subquery support via Condition.column_ref,
end-to-end compilation, param nesting, and fail-closed safety (no raw-string
operands, whitelisted comparators/quantifiers).
"""

import pytest

from dbbuddy_core.sql import (
    Condition,
    ExistsPredicate,
    InvalidConditionError,
    Predicate,
    QuantifiedPredicate,
    build_predicate,
    compile_parameterized_sql,
    compile_sql,
)


def _orders_subquery(where):
    return {"base_table": "orders", "select": [{"column": "*"}], "where": where}


# ── Predicate hierarchy ───────────────────────────────────────────────────────

def test_predicate_hierarchy():
    assert issubclass(Condition, Predicate)
    assert issubclass(ExistsPredicate, Predicate)
    assert issubclass(QuantifiedPredicate, Predicate)


# ── D1: EXISTS / NOT EXISTS ───────────────────────────────────────────────────

def test_exists_predicate_renders_both_modes():
    sub = _orders_subquery([{"column": "orders.total", "operator": ">", "value": 100}])
    p = ExistsPredicate(sub)
    assert p.render(parameterize=False) == (
        "EXISTS (SELECT * FROM orders WHERE orders.total > 100)", []
    )
    assert p.render(parameterize=True) == (
        "EXISTS (SELECT * FROM orders WHERE orders.total > %s)", [100]
    )


def test_not_exists_predicate():
    sub = _orders_subquery([{"column": "orders.total", "operator": ">", "value": 100}])
    frag, params = ExistsPredicate(sub, negated=True).render(parameterize=True)
    assert frag == "NOT EXISTS (SELECT * FROM orders WHERE orders.total > %s)"
    assert params == [100]


def test_build_predicate_dispatches_exists():
    assert isinstance(build_predicate({"operator": "EXISTS", "subquery": _orders_subquery([])}), ExistsPredicate)
    p = build_predicate({"operator": "NOT EXISTS", "subquery": _orders_subquery([])})
    assert isinstance(p, ExistsPredicate) and p.negated is True


def test_correlated_exists_end_to_end():
    # "customers who have placed an order over 100" — correlated via column_ref.
    plan = {"base_table": "customers", "select": [{"column": "*"}], "where": [
        {"operator": "EXISTS", "subquery": _orders_subquery([
            {"column": "orders.customer_id", "operator": "=", "column_ref": "customers.id"},
            {"column": "orders.total", "operator": ">", "value": 100},
        ])},
    ]}
    assert compile_sql(plan, engine="mysql") == (
        "SELECT * FROM customers WHERE EXISTS "
        "(SELECT * FROM orders WHERE orders.customer_id = customers.id AND orders.total > 100)"
    )
    assert compile_parameterized_sql(plan, engine="mysql") == (
        "SELECT * FROM customers WHERE EXISTS "
        "(SELECT * FROM orders WHERE orders.customer_id = customers.id AND orders.total > %s)",
        [100],
    )


def test_subquery_params_nest_in_where_order():
    plan = {"base_table": "customers", "select": [{"column": "*"}], "where": [
        {"column": "customers.status", "operator": "=", "value": "active"},
        {"operator": "EXISTS", "subquery": _orders_subquery(
            [{"column": "orders.total", "operator": ">", "value": 100}])},
        {"column": "customers.region", "operator": "=", "value": "EU"},
    ]}
    _sql, params = compile_parameterized_sql(plan, engine="mysql")
    assert params == ["active", 100, "EU"]


# ── column_ref (correlation) on Condition ─────────────────────────────────────

def test_column_ref_renders_column_to_column_without_params():
    cond = Condition("orders.customer_id", "=", column_ref="customers.id")
    assert cond.render(parameterize=True) == ("orders.customer_id = customers.id", [])
    assert cond.render(parameterize=False) == ("orders.customer_id = customers.id", [])


def test_from_spec_reads_column_ref():
    cond = Condition.from_spec({"column": "a.x", "operator": "=", "column_ref": "b.y"})
    assert cond.column_ref == "b.y"
    assert cond.render(parameterize=True) == ("a.x = b.y", [])


# ── D2: ANY / ALL ─────────────────────────────────────────────────────────────

def test_quantified_any_renders():
    sub = {"base_table": "competitor_prices", "select": [{"column": "competitor_prices.price"}]}
    p = QuantifiedPredicate("products.price", ">", "ANY", sub)
    assert p.render(parameterize=False) == (
        "products.price > ANY (SELECT competitor_prices.price FROM competitor_prices)", []
    )


def test_quantified_all_end_to_end_with_param():
    sub = {"base_table": "competitor_prices", "select": [{"column": "competitor_prices.price"}],
           "where": [{"column": "competitor_prices.region", "operator": "=", "value": "EU"}]}
    plan = {"base_table": "products", "select": [{"column": "*"}], "where": [
        {"column": "products.price", "comparator": ">=", "quantifier": "ALL", "subquery": sub},
    ]}
    assert compile_parameterized_sql(plan, engine="mysql") == (
        "SELECT * FROM products WHERE products.price >= ALL "
        "(SELECT competitor_prices.price FROM competitor_prices WHERE competitor_prices.region = %s)",
        ["EU"],
    )


def test_build_predicate_dispatches_quantifier():
    sub = {"base_table": "t", "select": [{"column": "*"}]}
    p = build_predicate({"column": "x", "comparator": "<", "quantifier": "all", "subquery": sub})
    assert isinstance(p, QuantifiedPredicate)


# ── fail-closed safety ────────────────────────────────────────────────────────

@pytest.mark.parametrize("subquery", ["SELECT 1 FROM orders", {}, None, 42])
def test_exists_rejects_non_plan_subquery(subquery):
    # A subquery operand must be an execution-plan dict — never a raw string.
    with pytest.raises(InvalidConditionError):
        ExistsPredicate(subquery).render(parameterize=True)


def test_quantified_rejects_bad_comparator_and_quantifier():
    sub = {"base_table": "t", "select": [{"column": "*"}]}
    with pytest.raises(InvalidConditionError):
        QuantifiedPredicate("x", "DROP", "ANY", sub).render(parameterize=True)
    with pytest.raises(InvalidConditionError):
        QuantifiedPredicate("x", ">", "SOME", sub).render(parameterize=True)
    with pytest.raises(InvalidConditionError):
        QuantifiedPredicate("", ">", "ANY", sub).render(parameterize=True)  # no column


def test_exists_with_raw_string_aborts_compilation():
    plan = {"base_table": "c", "select": [{"column": "*"}], "where": [
        {"operator": "EXISTS", "subquery": "SELECT 1 FROM orders"},
    ]}
    with pytest.raises(InvalidConditionError):
        compile_sql(plan)
