"""SQL generation subsystem.

Turns engine-agnostic execution plans into SQL. Layers (dependency order):

    renderer  →  operators  →  conditions  →  compiler

plus ``exceptions`` (typed errors). ``dbbuddy_core.execution`` re-exports
``compile_sql`` / ``compile_parameterized_sql`` from here for backward
compatibility; new code should import from ``dbbuddy_core.sql``.
"""

from dbbuddy_core.sql.compiler import compile_parameterized_sql, compile_sql
from dbbuddy_core.sql.conditions import Condition, build_predicate, render_conditions
from dbbuddy_core.sql.exceptions import (
    DialectCapabilityError,
    InvalidConditionError,
    SQLCompilationError,
    UnsupportedOperatorError,
)
from dbbuddy_core.sql.predicates import ExistsPredicate, Predicate, QuantifiedPredicate
from dbbuddy_core.sql.operators import (
    OPERATORS,
    BinaryOperator,
    Operator,
    RangeOperator,
    SetOperator,
    UnaryOperator,
    get_operator,
)
from dbbuddy_core.sql.renderer import render_literal

__all__ = [
    "compile_sql",
    "compile_parameterized_sql",
    "Condition",
    "render_conditions",
    "build_predicate",
    "Predicate",
    "ExistsPredicate",
    "QuantifiedPredicate",
    "Operator",
    "BinaryOperator",
    "UnaryOperator",
    "RangeOperator",
    "SetOperator",
    "get_operator",
    "OPERATORS",
    "render_literal",
    "SQLCompilationError",
    "InvalidConditionError",
    "UnsupportedOperatorError",
    "DialectCapabilityError",
]
