"""``Condition`` — a column + operator + value that renders itself.

``Condition`` is the common :class:`~dbbuddy_core.sql.predicates.Predicate` leaf;
the subquery predicates (EXISTS / ANY / ALL) live in ``predicates.py``.
``build_predicate`` dispatches a planner spec to the right one, and
``render_conditions`` turns a list of specs into WHERE fragments + bound params.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbbuddy_core.sql.operators import get_operator
from dbbuddy_core.sql.predicates import ExistsPredicate, Predicate, QuantifiedPredicate


@dataclass
class Condition(Predicate):
    """A single WHERE predicate — ``column operator value`` — that renders itself.

    ``value`` holds a scalar for binary operators, a ``[low, high]`` pair for
    ``BETWEEN``, and a collection for ``IN`` / ``NOT IN``; it is ignored for the
    null operators.

    ``column_ref``, when set, makes the right-hand side another *column* rather
    than a bound value — ``column operator column_ref`` — for column-to-column
    comparisons such as correlated-subquery joins (``orders.customer_id =
    customers.id``). The reference is a plan/schema identifier, interpolated like
    ``column`` and never bound.
    """

    column: str
    operator: str = "="
    value: Any = None
    column_ref: str | None = None

    def render(self, *, parameterize: bool, dialect=None) -> tuple[str, list]:
        if self.column_ref is not None:
            return f"{self.column} {self.operator} {self.column_ref}", []
        return get_operator(self.operator).render(
            self.column, self.value, parameterize=parameterize, dialect=dialect
        )

    @classmethod
    def from_spec(cls, spec: "Condition | dict") -> "Condition | None":
        """Normalize the planner's dict format into a :class:`Condition`.

        Backward-compatible with the existing ``{"column", "operator", "value"}``
        shape (a ``values`` alias and a ``column_ref`` operand are also accepted).
        Returns ``None`` for anything without a column so the compiler can skip it —
        mirroring the previous builder, which ignored column-less conditions.
        """
        if isinstance(spec, Condition):
            return spec
        if not isinstance(spec, dict):
            return None

        column = spec.get("column", "")
        if not column:
            return None

        operator = spec.get("operator", "=")

        # Column-to-column comparison (e.g. correlation) — no value binding.
        column_ref = spec.get("column_ref")
        if column_ref is not None:
            return cls(column=column, operator=operator, column_ref=column_ref)

        if "value" in spec:
            value = spec["value"]
        elif "values" in spec:
            value = spec["values"]
        else:
            value = ""  # legacy default: a missing value behaved like empty string

        # NULL coercion: a None value with = / != becomes an IS [NOT] NULL test,
        # exactly as the previous WHERE builder did. Other operators keep their
        # token and render "col OP NULL" through the binary operator.
        if value is None:
            if operator == "=":
                operator = "IS NULL"
            elif operator in ("!=", "<>"):
                operator = "IS NOT NULL"

        return cls(column=column, operator=operator, value=value)


def build_predicate(spec) -> Predicate | None:
    """Dispatch a WHERE spec to the right :class:`Predicate`, or ``None`` to skip.

    Recognizes the subquery predicates by their spec shape:

    * ``{"operator": "EXISTS"|"NOT EXISTS", "subquery": <plan>}`` → ExistsPredicate
    * ``{"column", "comparator", "quantifier": "ANY"|"ALL", "subquery": <plan>}``
      → QuantifiedPredicate

    Everything else falls through to :meth:`Condition.from_spec`.
    """
    if isinstance(spec, Predicate):
        return spec
    if not isinstance(spec, dict):
        return None

    op = str(spec.get("operator", "")).upper().strip()
    if op in ("EXISTS", "NOT EXISTS"):
        return ExistsPredicate(spec.get("subquery"), negated=(op == "NOT EXISTS"))

    if spec.get("quantifier"):
        return QuantifiedPredicate(
            column=spec.get("column", ""),
            comparator=spec.get("comparator", spec.get("operator", "=")),
            quantifier=spec.get("quantifier"),
            subquery=spec.get("subquery"),
        )

    return Condition.from_spec(spec)


def render_conditions(specs, *, parameterize: bool, dialect=None) -> tuple[list[str], list]:
    """Render an iterable of dict/Predicate specs into ``(fragments, params)``.

    ``dialect`` is passed through to each predicate for engine-specific rendering
    (e.g. ``ILIKE``, and subquery compilation for EXISTS/ANY/ALL).

    **Fail-closed:** a well-formed but uncompilable predicate raises
    :class:`~dbbuddy_core.sql.exceptions.InvalidConditionError`, which propagates
    and aborts compilation. A SQL compiler must refuse rather than silently drop a
    predicate and broaden the query. Structurally-empty specs (no column) carry no
    intent and are skipped — ``build_predicate`` returns ``None`` for them.
    """
    fragments: list[str] = []
    params: list = []
    for spec in specs:
        predicate = build_predicate(spec)
        if predicate is None:
            continue
        fragment, pred_params = predicate.render(parameterize=parameterize, dialect=dialect)
        fragments.append(fragment)
        params.extend(pred_params)
    return fragments, params
