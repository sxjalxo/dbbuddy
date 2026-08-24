"""Backward-compatibility shim.

The condition/operator framework moved into the ``dbbuddy_core.sql`` package
(``renderer`` / ``operators`` / ``conditions``). This module re-exports the
public names so existing imports (``from dbbuddy_core.sql_conditions import ...``)
keep working. Prefer importing from ``dbbuddy_core.sql`` in new code.
"""

from dbbuddy_core.sql.conditions import Condition, render_conditions
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
    "Condition",
    "render_conditions",
    "render_literal",
    "Operator",
    "BinaryOperator",
    "UnaryOperator",
    "RangeOperator",
    "SetOperator",
    "get_operator",
    "OPERATORS",
]
