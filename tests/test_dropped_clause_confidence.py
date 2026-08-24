"""Confidence must fall when the planner drops a clause the question asked for.

The engine's differentiator is that it can say *why* it wrote a query, and how
sure it is. That promise breaks in the worst possible way when a plan silently
loses a clause and still reports high confidence — the user is told the answer is
trustworthy precisely when it answers a different question than the one asked.

Observed on a fresh PostgreSQL schema, before this check existed:

    "total amount by region last quarter"
      -> SELECT SUM(payments.amount) FROM regions JOIN ... WHERE paid_at >= ...
         no grouping column, no GROUP BY, confidence: high

    "total revenue by region last quarter"
      -> SELECT regions.region_id FROM regions LIMIT 1001
         aggregation, measure and filter all gone, confidence: high

Both return a plausible-looking answer to a question nobody asked. The scoring
was bonus-only: it rewarded structure it *found* and had no term for structure it
had been asked for and failed to produce.

The check is a comparison between the intent and the compiled plan, so it stays
schema-agnostic — it never needs to know what a table or column is called.
"""

import pytest

from dbbuddy_core.query_planner import DROPPED_CLAUSE_PENALTY, detect_dropped_clauses


def _intent(**over):
    base = {
        "tables": ["payments"],
        "columns": [],
        "aggregation": None,
        "group_by": None,
        "filters": [],
        "limit": None,
        "order_by": None,
        "select": [],
        "original_query": "q",
    }
    base.update(over)
    return base


def _plan(**over):
    base = {
        "base_table": "payments",
        "select": [{"table": "payments", "column": "amount"}],
        "joins": [],
        "where": [],
        "group_by": [],
        "order_by": None,
        "aggregation": None,
        "limit": None,
    }
    base.update(over)
    return base


def test_nothing_dropped_when_plan_matches_intent():
    intent = _intent(aggregation={"function": "SUM"}, group_by=["segment"], filters=[{"c": 1}])
    plan = _plan(
        aggregation={"function": "SUM", "column": {"table": "payments", "column": "amount"}},
        group_by=[{"table": "customers", "column": "segment"}],
        where=[{"column": "paid_at", "operator": ">=", "value": "2026-04-01"}],
    )
    assert detect_dropped_clauses(intent, plan) == []


def test_no_false_positive_when_nothing_was_asked_for():
    """A plain "show me the customers" plan drops nothing — it was asked for nothing."""
    assert detect_dropped_clauses(_intent(), _plan()) == []


@pytest.mark.parametrize(
    "intent_over, plan_over, expected",
    [
        pytest.param(
            {"group_by": ["region"]}, {}, ["grouping"], id="grouping-dropped",
        ),
        pytest.param(
            {"aggregation": {"function": "SUM"}}, {}, ["aggregation"], id="aggregation-dropped",
        ),
        pytest.param(
            {"filters": [{"column": "paid_at"}]}, {}, ["filter"], id="filter-dropped",
        ),
        pytest.param(
            {"order_by": {"column": "amount"}}, {}, ["ordering"], id="ordering-dropped",
        ),
        pytest.param(
            {"aggregation": {"function": "SUM"}, "group_by": ["region"], "filters": [{"c": 1}]},
            {},
            ["aggregation", "grouping", "filter"],
            id="the-total-collapse",
        ),
    ],
)
def test_dropped_clauses_are_reported(intent_over, plan_over, expected):
    dropped = detect_dropped_clauses(_intent(**intent_over), _plan(**plan_over))
    assert dropped == expected


def test_penalty_is_decisive_not_cosmetic():
    """One dropped clause must not leave the answer looking merely 'medium'.

    Levels are high >= 0.8, medium >= 0.5. A typical base score is 0.85, so the
    penalty has to be large enough to push a single drop below 0.5 — a query that
    answers a different question is not a middling answer, it is a wrong one.

    It is still a taper, not a cut-off: the plan is returned, with the reason
    attached, rather than suppressed.
    """
    assert 0.85 - DROPPED_CLAUSE_PENALTY < 0.5
    assert DROPPED_CLAUSE_PENALTY < 0.85, "a single drop should not floor the score at zero"


def test_grouping_survives_when_plan_groups_by_something_else():
    """The check is about presence, not agreement.

    Choosing the wrong grouping column is a different defect with a different
    signal (ambiguity). This check only fires when the clause is *absent*, so it
    stays cheap and has no schema opinions.
    """
    intent = _intent(group_by=["region"])
    plan = _plan(group_by=[{"table": "customers", "column": "segment"}])
    assert detect_dropped_clauses(intent, plan) == []


def test_grouping_dropped_is_detected_from_the_select_dimension():
    """Grouping lives in `intent["select"]`, not `intent["group_by"]`.

    The intent builder keeps the dimension as a plain column in the select list
    and lets the planner's structural pass turn it into the GROUP BY. A check
    reading only `intent["group_by"]` therefore never sees grouping — which is
    how "total amount by region" returned a single global total at high
    confidence.
    """
    intent = _intent(aggregation={"function": "SUM"}, _requested_grouping=True)
    plan = _plan(
        aggregation={"function": "SUM", "column": {"table": "payments", "column": "amount"}},
        group_by=[],
    )
    assert detect_dropped_clauses(intent, plan) == ["grouping"]


def test_select_dimension_present_and_grouped_is_not_a_drop():
    intent = _intent(aggregation={"function": "SUM"}, _requested_grouping=True)
    plan = _plan(
        aggregation={"function": "SUM", "column": {"table": "payments", "column": "amount"}},
        group_by=[{"table": "regions", "column": "region_name"}],
    )
    assert detect_dropped_clauses(intent, plan) == []


def test_aggregate_only_select_is_not_a_grain():
    """"total revenue" selects only the aggregate — no grain was ever asked for."""
    intent = _intent(aggregation={"function": "SUM"}, _requested_grouping=False)
    plan = _plan(aggregation={"function": "SUM", "column": {"table": "payments", "column": "amount"}})
    assert detect_dropped_clauses(intent, plan) == []


def test_the_aggregates_own_column_is_not_a_grain():
    """"how many customers" keeps the counted column in the select list.

    Reading that as a requested dimension reported grouping dropped for a
    question that asked for a single number — penalising the most ordinary query
    there is, and (because caching is gated on confidence) quietly disabling the
    response cache for it.
    """
    intent = _intent(
        aggregation={"function": "COUNT"},
        # Retrieval residue: the select list is not the grouping signal.
        select=[{"table": "customers", "column": "name", "aggregation": None}],
        _requested_grouping=False,
    )
    plan = _plan(
        aggregation={"function": "COUNT", "column": {"table": "customers", "column": "id"}},
        group_by=[],
    )
    assert detect_dropped_clauses(intent, plan) == []


def test_requests_grouping_detects_the_phrase_not_the_schema():
    from dbbuddy_core.intent_builder import requests_grouping

    assert requests_grouping("total amount by region")
    assert requests_grouping("revenue per customer")
    assert requests_grouping("count for each status")
    assert not requests_grouping("how many customers are there")
    assert not requests_grouping("total revenue")
