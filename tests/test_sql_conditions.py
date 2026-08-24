"""Contract tests for the Condition/Operator framework (dbbuddy_core.sql_conditions).

This is the behavior-preserving first pass: only the operators the previous
hand-written WHERE builder supported are covered (=, comparison operators, LIKE,
IS [NOT] NULL via None coercion, BETWEEN). IN/NOT IN and EXISTS are deliberately
not here — they arrive in later milestones.
"""

import pytest

from dbbuddy_core.sql_conditions import (
    BinaryOperator,
    Condition,
    OPERATORS,
    RangeOperator,
    SetOperator,
    UnaryOperator,
    get_operator,
    render_conditions,
    render_literal,
)


# ── render_literal ────────────────────────────────────────────────────────────

def test_render_literal_quotes_and_escapes_strings():
    assert render_literal("o'brien") == "'o''brien'"
    assert render_literal(42) == "42"
    assert render_literal(3.5) == "3.5"


# ── operator lookup ───────────────────────────────────────────────────────────

def test_registry_lookup_is_case_insensitive():
    assert isinstance(get_operator("between"), RangeOperator)
    assert isinstance(get_operator("BETWEEN"), RangeOperator)
    assert isinstance(get_operator("is null"), UnaryOperator)
    assert isinstance(get_operator("LIKE"), BinaryOperator)


def test_unknown_operator_falls_back_to_verbatim_binary():
    # Preserves the historical pass-through: an operator the compiler was never
    # taught (here REGEXP) is still rendered as a binary infix, token unchanged.
    # (ILIKE used to be the example here — it is now a first-class operator.)
    op = get_operator("REGEXP")
    assert op.render("t.a", "x", parameterize=True) == ("t.a REGEXP %s", ["x"])
    assert op.render("t.a", "x", parameterize=False) == ("t.a REGEXP 'x'", [])


# ── per-operator rendering (both modes) ───────────────────────────────────────

@pytest.mark.parametrize("operator,value,inline,param_sql,params", [
    ("=", "x", "t.a = 'x'", "t.a = %s", ["x"]),
    (">", 5, "t.a > 5", "t.a > %s", [5]),
    ("LIKE", "%x%", "t.a LIKE '%x%'", "t.a LIKE %s", ["%x%"]),
    ("BETWEEN", [20, 30], "t.a BETWEEN 20 AND 30", "t.a BETWEEN %s AND %s", [20, 30]),
])
def test_operator_renders_both_modes(operator, value, inline, param_sql, params):
    cond = Condition("t.a", operator, value)
    assert cond.render(parameterize=False) == (inline, [])
    assert cond.render(parameterize=True) == (param_sql, params)


def test_between_rejects_non_pair_value():
    with pytest.raises(ValueError):
        Condition("t.a", "BETWEEN", 42).render(parameterize=True)
    with pytest.raises(ValueError):
        Condition("t.a", "BETWEEN", [1, 2, 3]).render(parameterize=False)


# ── from_spec: backward compatibility with the planner dict format ────────────

def test_from_spec_reads_legacy_dict():
    cond = Condition.from_spec({"column": "t.a", "operator": ">", "value": 3})
    assert (cond.column, cond.operator, cond.value) == ("t.a", ">", 3)


def test_from_spec_accepts_values_alias():
    cond = Condition.from_spec({"column": "t.a", "operator": "BETWEEN", "values": [1, 9]})
    assert cond.value == [1, 9]


def test_from_spec_missing_value_defaults_to_empty_string():
    # The previous builder used condition.get("value", "") — preserved here.
    cond = Condition.from_spec({"column": "t.a"})
    assert cond.render(parameterize=True) == ("t.a = %s", [""])


def test_from_spec_without_column_is_dropped():
    assert Condition.from_spec({"operator": "=", "value": "x"}) is None
    assert Condition.from_spec("not-a-dict") is None


@pytest.mark.parametrize("operator,expected", [
    ("=", "t.a IS NULL"),
    ("!=", "t.a IS NOT NULL"),
    ("<>", "t.a IS NOT NULL"),
])
def test_none_value_coerces_to_null_test(operator, expected):
    cond = Condition.from_spec({"column": "t.a", "operator": operator, "value": None})
    assert cond.render(parameterize=True) == (expected, [])
    assert cond.render(parameterize=False) == (expected, [])


def test_none_value_with_other_operator_renders_literal_null():
    # Legacy fallthrough: "col OP NULL" with nothing bound, in both modes.
    cond = Condition.from_spec({"column": "t.a", "operator": ">", "value": None})
    assert cond.render(parameterize=True) == ("t.a > NULL", [])
    assert cond.render(parameterize=False) == ("t.a > NULL", [])


# ── render_conditions: AND joining + fail-closed on invalid ───────────────────

def test_render_conditions_collects_fragments_and_params_in_order():
    specs = [
        {"column": "t.a", "operator": "=", "value": "x"},
        {"column": "t.b", "operator": "BETWEEN", "value": [1, 9]},
    ]
    frags, params = render_conditions(specs, parameterize=True)
    assert frags == ["t.a = %s", "t.b BETWEEN %s AND %s"]
    assert params == ["x", 1, 9]


def test_render_conditions_skips_structurally_empty_specs():
    # No-column / non-dict specs carry no intent — they are skipped, not raised.
    specs = [
        {"column": "t.a", "operator": "=", "value": "x"},
        {"operator": "=", "value": "y"},   # no column → skipped
        "not-a-dict",                       # not a dict → skipped
    ]
    frags, params = render_conditions(specs, parameterize=True)
    assert frags == ["t.a = %s"]
    assert params == ["x"]


def test_render_conditions_aborts_on_invalid_condition():
    # Fail-closed: a well-formed but uncompilable condition raises rather than
    # being silently dropped (which would broaden the query).
    from dbbuddy_core.sql import InvalidConditionError

    specs = [
        {"column": "t.a", "operator": "=", "value": "x"},
        {"column": "t.b", "operator": "BETWEEN", "value": 42},  # invalid → abort
    ]
    with pytest.raises(InvalidConditionError):
        render_conditions(specs, parameterize=True)


# ── IN / NOT IN (SetOperator) ─────────────────────────────────────────────────

def test_in_registered_and_renders_both_modes():
    assert isinstance(get_operator("IN"), SetOperator)
    cond = Condition("t.a", "IN", [1, 2, 3])
    assert cond.render(parameterize=False) == ("t.a IN (1, 2, 3)", [])
    assert cond.render(parameterize=True) == ("t.a IN (%s, %s, %s)", [1, 2, 3])


def test_not_in_renders_both_modes():
    cond = Condition("t.a", "NOT IN", ["a", "b"])
    assert cond.render(parameterize=False) == ("t.a NOT IN ('a', 'b')", [])
    assert cond.render(parameterize=True) == ("t.a NOT IN (%s, %s)", ["a", "b"])


@pytest.mark.parametrize("value", [5, (5,), {5}, [5]])
def test_in_normalizes_scalar_and_collections_to_single_predicate(value):
    # scalar, tuple, set, list of one → the same single-element predicate
    assert Condition("t.a", "IN", value).render(parameterize=True) == ("t.a IN (%s)", [5])


def test_empty_set_uses_constant_predicate():
    # IN () -> 1=0 (matches nothing); NOT IN () -> 1=1 (matches everything)
    assert Condition("t.a", "IN", []).render(parameterize=True) == ("1=0", [])
    assert Condition("t.a", "NOT IN", []).render(parameterize=True) == ("1=1", [])
    assert Condition("t.a", "IN", ()).render(parameterize=False) == ("1=0", [])


def test_null_allowed_in_in_but_rejected_in_not_in():
    # NULL is fine in IN (never matches) ...
    assert Condition("t.a", "IN", [1, None]).render(parameterize=True) == (
        "t.a IN (%s, %s)", [1, None]
    )
    # ... but a validation error in NOT IN (three-valued logic footgun).
    with pytest.raises(ValueError):
        Condition("t.a", "NOT IN", [1, None]).render(parameterize=True)
    with pytest.raises(ValueError):
        Condition("t.a", "NOT IN", [None]).render(parameterize=False)


def test_not_in_with_null_aborts_render_conditions():
    # Fail-closed: a NOT IN containing NULL raises rather than being dropped.
    from dbbuddy_core.sql import InvalidConditionError

    specs = [
        {"column": "t.a", "operator": "IN", "values": [1, 2]},
        {"column": "t.b", "operator": "NOT IN", "value": [3, None]},  # invalid → abort
    ]
    with pytest.raises(InvalidConditionError):
        render_conditions(specs, parameterize=True)


# ── frozen registry ───────────────────────────────────────────────────────────

def test_operator_registry_is_read_only():
    with pytest.raises(TypeError):
        OPERATORS["FOO"] = get_operator("=")  # type: ignore[index]


def test_no_public_mutation_api():
    import dbbuddy_core.sql_conditions as m
    # Registration is import-time only; no public mutator is exposed.
    assert not hasattr(m, "register_operator")


# ── typed exceptions (dbbuddy_core.sql.exceptions) ────────────────────────────

def test_invalid_condition_error_hierarchy_and_raises():
    from dbbuddy_core.sql import InvalidConditionError, SQLCompilationError

    # A SQL-compilation error, and still a ValueError for backward compatibility.
    assert issubclass(InvalidConditionError, SQLCompilationError)
    assert issubclass(InvalidConditionError, ValueError)

    # The operators that reject their operand raise the typed error.
    with pytest.raises(InvalidConditionError):
        Condition("t.a", "BETWEEN", 42).render(parameterize=True)
    with pytest.raises(InvalidConditionError):
        Condition("t.a", "NOT IN", [None]).render(parameterize=True)


# ── ILIKE (dialect-rendered) ──────────────────────────────────────────────────

class _RecordingDialect:
    """Minimal duck-typed dialect for exercising ILIKE delegation without a driver."""

    def __init__(self, template):
        self.template = template
        self.calls = []

    def render_ilike(self, column, rhs):
        self.calls.append((column, rhs))
        return self.template.format(col=column, rhs=rhs)


def test_ilike_is_registered_operator():
    from dbbuddy_core.sql.operators import ILikeOperator

    assert isinstance(get_operator("ILIKE"), ILikeOperator)
    assert isinstance(get_operator("ilike"), ILikeOperator)  # case-insensitive


def test_ilike_delegates_to_dialect_and_owns_param_mechanics():
    d = _RecordingDialect("{col} ILIKE {rhs}")
    op = get_operator("ILIKE")
    # Operator owns %s-vs-literal + params; dialect owns the fragment shape.
    assert op.render("t.a", "x%", parameterize=True, dialect=d) == ("t.a ILIKE %s", ["x%"])
    assert op.render("t.a", "x%", parameterize=False, dialect=d) == ("t.a ILIKE 'x%'", [])
    assert d.calls == [("t.a", "%s"), ("t.a", "'x%'")]


def test_ilike_without_dialect_uses_portable_rewrite():
    # No engine context → portable LOWER() rewrite, never the "col ILIKE %s"
    # passthrough (a syntax error on MySQL).
    op = get_operator("ILIKE")
    assert op.render("t.a", "x", parameterize=True, dialect=None) == ("LOWER(t.a) LIKE LOWER(%s)", ["x"])
    assert op.render("t.a", "x", parameterize=False, dialect=None) == ("LOWER(t.a) LIKE LOWER('x')", [])


def test_render_ilike_mysql_rewrites_to_lower():
    from dbbuddy_core.dialects.registry import get_dialect

    d = get_dialect("mysql")
    assert d.capabilities.supports_ilike is False
    assert d.render_ilike("t.a", "%s") == "LOWER(t.a) LIKE LOWER(%s)"


def test_render_ilike_postgres_is_native():
    pytest.importorskip("psycopg2")
    from dbbuddy_core.dialects.registry import get_dialect

    d = get_dialect("postgresql")
    assert d.capabilities.supports_ilike is True
    assert d.render_ilike("t.a", "%s") == "t.a ILIKE %s"


def test_ilike_end_to_end_mysql_rewrite():
    # get_dialect("mysql") gives supports_ilike=False; even if the driver were
    # absent the defensive fallback yields the same portable rewrite.
    from dbbuddy_core.execution import compile_sql, compile_parameterized_sql

    plan = {"base_table": "users", "select": [{"column": "*"}],
            "where": [{"column": "users.name", "operator": "ILIKE", "value": "%jo%"}]}
    assert compile_sql(plan, engine="mysql") == (
        "SELECT * FROM users WHERE LOWER(users.name) LIKE LOWER('%jo%')"
    )
    assert compile_parameterized_sql(plan, engine="mysql") == (
        "SELECT * FROM users WHERE LOWER(users.name) LIKE LOWER(%s)", ["%jo%"]
    )


def test_ilike_end_to_end_postgres_native():
    pytest.importorskip("psycopg2")
    from dbbuddy_core.execution import compile_sql

    plan = {"base_table": "users", "select": [{"column": "*"}],
            "where": [{"column": "users.name", "operator": "ILIKE", "value": "%jo%"}]}
    assert compile_sql(plan, engine="postgresql") == (
        "SELECT * FROM users WHERE users.name ILIKE '%jo%'"
    )
