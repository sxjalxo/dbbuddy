"""Probe suites for the hospital dataset.

Same invariant discipline, aimed at what this schema stresses: name-heuristic
joins (no declared FKs), singular table names, deep paths, a composite key with
no surrogate ``id``, and columns whose names collide across tables.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.suites import SuiteContext  # reuse the probe/check plumbing


def suite_basics(ctx: SuiteContext) -> None:
    probes = [ctx.probe(q) for q in (
        "how many patients", "how many encounters", "how many practitioners",
        "list practices", "show regions")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_golden(ctx.probe("how many patients"),
                               ctx.truth("SELECT COUNT(*) FROM patient")))
    ctx.check(inv.check_golden(ctx.probe("how many encounters"),
                               ctx.truth("SELECT COUNT(*) FROM encounter")))


def suite_singular_tables(ctx: SuiteContext) -> None:
    """Every table here is singular — the form name-based inference used to drop."""
    probes = [ctx.probe(q) for q in (
        "how many diagnosis", "list patient", "count observation")]
    ctx.check(inv.check_executes(probes))


def suite_undeclared_joins(ctx: SuiteContext) -> None:
    """No FK is declared anywhere, so every join is inferred from column names."""
    probes = [ctx.probe(q) for q in (
        "encounters by patient sex",
        "how many encounters per speciality",
        "patients by region",
        "observations by encounter kind")]
    ctx.check(inv.check_executes(probes))


def suite_deep_path(ctx: SuiteContext) -> None:
    """observation -> encounter -> patient -> practice -> region is four hops."""
    probes = [ctx.probe(q) for q in (
        "observations by region", "encounters by practice name",
        "total cost by region")]
    ctx.check(inv.check_executes(probes))


def suite_fanout(ctx: SuiteContext) -> None:
    total = ctx.probe("total cost")
    by_kind = ctx.probe("total cost by kind")
    ctx.check(inv.check_executes([total, by_kind]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_kind))
    ctx.check(inv.check_golden(total, ctx.truth("SELECT SUM(cost) FROM encounter")))

    # A measure on the far side of a many-to-many with payload.
    total_minutes = ctx.probe("total duration minutes")
    ctx.check(inv.check_golden(
        total_minutes, ctx.truth("SELECT SUM(duration_minutes) FROM encounter")))


def suite_signed_measures(ctx: SuiteContext) -> None:
    """`adjustment` is legitimately negative and legitimately zero — "0" is a real
    answer here, so a wrong 0 cannot be spotted by eye."""
    total = ctx.probe("total adjustment")
    ctx.check(inv.check_executes([total]))
    ctx.check(inv.check_golden(total, ctx.truth("SELECT SUM(adjustment) FROM encounter")))


def suite_composite_key(ctx: SuiteContext) -> None:
    """`encounter_diagnosis` has no `id`; COUNT must not assume one exists."""
    probes = [ctx.probe(q) for q in (
        "how many encounter diagnosis", "diagnoses by rank")]
    ctx.check(inv.check_executes(probes))


def suite_ambiguous_columns(ctx: SuiteContext) -> None:
    """`name` exists on region, practice, patient, practitioner and diagnosis;
    `code` on diagnosis and observation. An unqualified reference is ambiguous."""
    probes = [ctx.probe(q) for q in (
        "list patient name", "diagnosis code", "practice name")]
    ctx.check(inv.check_executes(probes))


def suite_extremes_and_filters(ctx: SuiteContext) -> None:
    highest = ctx.probe("highest cost")
    ctx.check(inv.check_executes([highest]))
    ctx.check(inv.check_golden(highest, ctx.truth("SELECT MAX(cost) FROM encounter")))

    lowest = ctx.probe("lowest birth year")
    ctx.check(inv.check_golden(lowest, ctx.truth("SELECT MIN(birth_year) FROM patient")))

    unfiltered = ctx.probe("how many encounters")
    for question in ("how many urgent encounters",
                     "how many encounters with kind routine",
                     "count telehealth encounters"):
        filtered = ctx.probe(question)
        ctx.check(inv.check_executes([filtered]))
        ctx.check(inv.check_filter_narrows(unfiltered, filtered))

    ctx.check(inv.check_golden(
        ctx.probe("how many urgent encounters"),
        ctx.truth("SELECT COUNT(*) FROM encounter WHERE kind = 'urgent'")))


def suite_paraphrase(ctx: SuiteContext) -> None:
    for group in (
        ["how many patients", "count patients", "total number of patients"],
        ["how many urgent encounters", "count encounters with kind urgent",
         "number of urgent encounters"],
        ["total cost", "sum of cost", "total encounter cost"],
    ):
        probes = [ctx.probe(q) for q in group]
        ctx.check(inv.check_executes(probes))
        ctx.check(inv.check_paraphrases_agree(probes))


def suite_cross_table_measures(ctx: SuiteContext) -> None:
    """Measures that live one or more hops from the entity being grouped.

    The hardest correctness case in the product: the measure is on the child, the
    grain is on the parent, and any plan that joins before aggregating inflates
    the number by the child fan-out.
    """
    for total_q, grouped_q in (
        ("total value numeric", "total value numeric by code"),
        ("total duration minutes", "total duration minutes by kind"),
        # `code` exists on both `observation` and `diagnosis`, so a bare "by code"
        # is ambiguous and either reading is defensible. Qualified here because
        # this suite probes fan-out, not disambiguation — see the ambiguity suite.
        ("how many observations", "how many observations by observation code"),
    ):
        total, grouped = ctx.probe(total_q), ctx.probe(grouped_q)
        ctx.check(inv.check_executes([total, grouped]))
        ctx.check(inv.check_breakdown_sums_to_total(total, grouped))

    # Ground truth for a measure reached only through a junction table.
    ctx.check(inv.check_golden(
        ctx.probe("how many observations"),
        ctx.truth("SELECT COUNT(*) FROM observation")))


def suite_ambiguity_pressure(ctx: SuiteContext) -> None:
    """`name` exists on five tables and `code` on two. A bare reference is
    genuinely ambiguous, so the engine must either qualify it or decline —
    silently picking one table is the failure mode."""
    probes = [ctx.probe(q) for q in (
        "how many name", "count code", "list name and code",
        "diagnosis name by code")]
    ctx.check(inv.check_executes(probes))


def suite_stress(ctx: SuiteContext) -> None:
    from scripts.dogfood.invariants import Finding

    probes = [ctx.probe(q) for q in (
        "how many urgent encounters for active patients in the northern region "
        "seen by a cardiology practitioner last year",
        "cost",
        "patient'; DROP TABLE encounter; --",
        "how many " + "chronic " * 40 + "diagnoses",
        "SELECT * FROM patient",
        "encounters where cost > 100 and cost < 50",
        "average of the average cost",
    )]
    for p in probes:
        if p.sql and any(kw in p.sql.upper().split()
                         for kw in ("DROP", "DELETE", "UPDATE", "INSERT", "TRUNCATE", "ALTER")):
            ctx.check([Finding("EXECUTES", "generated a write",
                               "a read-only question compiled to a write statement", [p])])


def suite_hard(ctx: SuiteContext) -> None:
    """Higher-stress shapes: multi-hop grouping, NULL-bearing measures, negative
    values, and aggregates over a table reachable only through a junction."""
    # A measure with NULLs: SUM/AVG must skip them, COUNT(col) must not count them.
    total_obs = ctx.probe("total value numeric")
    ctx.check(inv.check_executes([total_obs]))
    ctx.check(inv.check_golden(
        total_obs, ctx.truth("SELECT SUM(value_numeric) FROM observation")))

    avg_duration = ctx.probe("average duration minutes")
    ctx.check(inv.check_golden(
        avg_duration, ctx.truth("SELECT AVG(duration_minutes) FROM encounter")))

    # Grouped breakdowns must still total, including across a join.
    for total_q, grouped_q in (
        ("total cost", "total cost by kind"),
        ("total adjustment", "total adjustment by kind"),
        ("how many encounters", "how many encounters by kind"),
    ):
        total, grouped = ctx.probe(total_q), ctx.probe(grouped_q)
        ctx.check(inv.check_executes([total, grouped]))
        ctx.check(inv.check_breakdown_sums_to_total(total, grouped))

    # Extremes on a signed measure — MIN must be the negative one.
    ctx.check(inv.check_golden(
        ctx.probe("lowest adjustment"),
        ctx.truth("SELECT MIN(adjustment) FROM encounter")))

    # Counting a table with no surrogate id.
    ctx.check(inv.check_golden(
        ctx.probe("how many encounter diagnosis"),
        ctx.truth("SELECT COUNT(*) FROM encounter_diagnosis")))


def suite_filter_stability(ctx: SuiteContext) -> None:
    """A filter must narrow, and a redundant one must not move the answer."""
    base = ctx.probe("how many encounters")
    for narrower in ("how many routine encounters",
                     "how many emergency encounters",
                     "how many encounters with kind follow-up"):
        p = ctx.probe(narrower)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_filter_narrows(base, p))

    # Same question, three ways — all must agree.
    group = [ctx.probe(q) for q in (
        "how many routine encounters",
        "count encounters with kind routine",
        "number of routine encounters")]
    ctx.check(inv.check_executes(group))
    ctx.check(inv.check_paraphrases_agree(group))
    ctx.check(inv.check_golden(
        group[0], ctx.truth("SELECT COUNT(*) FROM encounter WHERE kind = 'routine'")))


SUITES: dict[str, Callable[[SuiteContext], None]] = {
    "basics": suite_basics,
    "singular_tables": suite_singular_tables,
    "undeclared_joins": suite_undeclared_joins,
    "deep_path": suite_deep_path,
    "fanout": suite_fanout,
    "signed_measures": suite_signed_measures,
    "composite_key": suite_composite_key,
    "ambiguous_columns": suite_ambiguous_columns,
    "extremes_and_filters": suite_extremes_and_filters,
    "paraphrase": suite_paraphrase,
    "hard": suite_hard,
    "cross_table_measures": suite_cross_table_measures,
    "ambiguity_pressure": suite_ambiguity_pressure,
    "filter_stability": suite_filter_stability,
    "stress": suite_stress,
}
