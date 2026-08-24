"""Adversarial dogfood for the Insights Engine, added after exercising it against
the real employees dataset (~4M rows) ahead of the ERP rollout.

The unit suite in ``test_insights_engine.py`` proves each guard in isolation.
These add the cases that only show up when a *realistic, messy model response* is
run through the whole service path at once — the mixed valid/hallucinated bundle,
scale-sensitive statistics, and one documented guardrail gap tracked as xfail so
it is visible rather than forgotten.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dbbuddy_core import ai_providers as aip
from dbbuddy_core.insights import context as ctxmod
from dbbuddy_core.insights.prompts import CANNOT_DETERMINE
from dbbuddy_core.insights.service import answer_followup, generate_insights
from dbbuddy_core.insights.validators import ungrounded_causal_claim


# ── scripted provider seam (mirrors test_insights_engine._Scripted) ──────────
class _Scripted(aip.AIProvider):
    adapter = "scripted"
    script: dict = {}
    calls: list = []

    def generate(self, prompt, *, temperature=None, json_mode=None):
        type(self).calls.append(prompt)
        item = type(self).script[self.config.name].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def scripted(monkeypatch):
    _Scripted.script, _Scripted.calls = {}, []
    monkeypatch.setattr(aip, "get_provider", lambda cfg: _Scripted(cfg))
    return _Scripted


def _cfg(name):
    return aip.ProviderRuntimeConfig(adapter="scripted", model="m", name=name)


GENDER_ROWS = [
    {"gender": "F", "total_salary": Decimal("72552335529")},
    {"gender": "M", "total_salary": Decimal("108928421890")},
]


def _ctx(rows=GENDER_ROWS):
    return ctxmod.build_context(
        question="total salary by gender", sql="SELECT gender, SUM(salary) ...",
        database="employees", rows=rows,
    )


def test_mixed_bundle_keeps_only_the_grounded_finding(scripted):
    """A single model response mixing one grounded finding with a weather cause,
    an ungrounded 'headwinds' cause, and an uncited claim must come back with
    exactly the grounded finding and a limitation explaining the removals."""
    import json
    scripted.script = {"A": [json.dumps({
        "summary": "Male total_salary exceeds female total_salary.",
        "findings": [
            {"title": "Gap", "detail": "total_salary is higher for M",
             "evidence": "total_salary M > F"},
            {"title": "Weather", "detail": "the gap is because of the weather",
             "evidence": "x"},
            {"title": "Econ", "detail": "driven by macroeconomic headwinds",
             "evidence": "total_salary"},
            {"title": "NoCite", "detail": "salaries seem high", "evidence": ""},
        ],
        "recommendations": ["Investigate the total_salary gap",
                            "Adjust for the recession"],
    })]}
    bundle = generate_insights(_ctx(), [_cfg("A")])

    assert [f.title for f in bundle.findings] == ["Gap"]
    assert bundle.limitations  # removals disclosed, never silent
    assert any("gap" in r.lower() for r in bundle.recommendations)
    assert all("recession" not in r.lower() for r in bundle.recommendations)


def test_leading_followup_cannot_extract_an_invented_cause(scripted):
    scripted.script = {"A": ['{"answer":"Because male employees negotiate harder.",'
                             '"evidence":""}']}
    result = answer_followup(_ctx(), "Why do men earn more?", [_cfg("A")])
    assert result["answer"] == CANNOT_DETERMINE


def test_statistics_are_exact_at_erp_scale():
    """Decimal money and billion-scale sums must not drift through float."""
    money = [{"amt": Decimal("0.01")} for _ in range(1000)]
    col = ctxmod.build_context(question="q", sql="s", database="d",
                               rows=money).columns[0]
    assert abs(col.total - 10.0) < 1e-9

    big = [{"salary": 10**9}, {"salary": 2 * 10**9}, {"salary": 3 * 10**9}]
    col = ctxmod.build_context(question="q", sql="s", database="d",
                               rows=big).columns[0]
    assert col.total == 6 * 10**9 and col.mean == 2 * 10**9


def test_result_hash_is_data_identity_not_question():
    a = ctxmod.build_context(question="phrasing one", sql="S", database="d", rows=GENDER_ROWS)
    b = ctxmod.build_context(question="totally different phrasing", sql="S", database="d", rows=GENDER_ROWS)
    changed = ctxmod.build_context(question="phrasing one", sql="S", database="d",
                                   rows=[{"gender": "F", "total_salary": 1}, GENDER_ROWS[1]])
    assert ctxmod.result_hash(a) == ctxmod.result_hash(b)          # question-independent
    assert ctxmod.result_hash(a) != ctxmod.result_hash(changed)    # data-sensitive


def test_fabricated_number_does_not_launder_an_external_cause():
    """H1, fixed: with the result's actual values supplied, a fabricated figure
    ("47 supply-chain disruptions") no longer grounds an external cause."""
    from dbbuddy_core.insights.context import grounded_numbers

    ctx = _ctx()
    numbers = grounded_numbers(ctx)
    columns = [c.name for c in ctx.columns]

    # Fabricated number, no column named -> rejected.
    assert ungrounded_causal_claim(
        "sales collapsed because of 47 supply-chain disruptions", columns, numbers)

    # A real result value (row_count is 2) still grounds a clause that cites it.
    assert not ungrounded_causal_claim(
        "the split reflects the 2 gender groups", columns, numbers)

    # Columns-only call stays permissive (no result values to check against), so
    # the unit-suite contract and callers that cannot see values are unaffected.
    assert not ungrounded_causal_claim(
        "sales collapsed because of 47 supply-chain disruptions", columns)


def test_fabricated_number_blocked_end_to_end(scripted):
    """Through the full service path, a finding blaming a fabricated figure is
    removed and the removal disclosed."""
    import json
    scripted.script = {"A": [json.dumps({
        "summary": "",
        "findings": [{"title": "Bad", "detail":
                      "the gap exists because of 47 supply-chain disruptions",
                      "evidence": "total_salary"}],
    })]}
    bundle = generate_insights(_ctx(), [_cfg("A")])
    assert bundle.findings == []
    assert any("does not contain" in lim for lim in bundle.limitations)
