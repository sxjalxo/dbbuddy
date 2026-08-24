"""Probe suites for MySQL's AirportDB — real data, real scale.

The seven datasets before this one each isolated a *kind* of confusion. This one
puts several of them on the same schema at once, with real rows underneath:

* **Role-playing dimensions that are also reserved words.** ``flight.from`` and
  ``flight.to`` both reference ``airport``. TPC-DS proved the role selection on a
  benchmark schema; here the same columns must survive quoting as well
  ("departure airport" vs "arrival airport" — and neither may become a syntax
  error).
* **A 1:1 detail table.** ``passengerdetails`` hangs off ``passenger`` one-to-one,
  a join shape no other dataset in the suite has; a 1:1 join must not change any
  count.
* **Scale.** ``booking`` is the largest table in the whole suite. A fan-out here
  is not only a wrong number, it is a query that does not come back.
* **Denormalised history** (``flight_log``, with ``*_old``/``*_new`` pairs) and a
  **time series** (``weatherdata``) beside the operational tables.

Golden values come from the loaded database (``ctx.truth``), so they hold at any
tier — the suite does not care whether the build capped rows.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.invariants import Finding
from scripts.dogfood.suites import SuiteContext


def suite_basics(ctx: SuiteContext) -> None:
    probes = [ctx.probe(q) for q in (
        "how many airlines", "how many airports", "how many passengers",
        "how many flights", "how many bookings", "how many employees",
        "list airplane types")]
    ctx.check(inv.check_executes(probes))
    for question, table in (("how many airlines", "airline"),
                            ("how many airports", "airport"),
                            ("how many passengers", "passenger"),
                            ("how many flights", "flight"),
                            ("how many bookings", "booking"),
                            ("how many employees", "employee")):
        ctx.check(inv.check_golden(ctx.probe(question),
                                   ctx.truth(f"SELECT COUNT(*) FROM {table}")))


def suite_measures(ctx: SuiteContext) -> None:
    """Money and quantities on real tables."""
    cases = [
        ("total booking price", "SELECT SUM(price) FROM booking"),
        ("average booking price", "SELECT AVG(price) FROM booking"),
        ("highest employee salary", "SELECT MAX(salary) FROM employee"),
        ("average airplane capacity", "SELECT AVG(capacity) FROM airplane"),
    ]
    for question, truth_sql in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


def suite_reserved_word_columns(ctx: SuiteContext) -> None:
    """``from`` and ``to`` are column names here. Any plan touching them must be
    quoted, and must not be mistaken for SQL keywords."""
    probes = [ctx.probe(q) for q in (
        "how many flights per airline",
        "list flights with their departure and arrival",
        "how many flight schedules")]
    ctx.check(inv.check_executes(probes))
    for p in probes:
        if p.sql and (" from from " in f" {p.sql.lower()} "
                      or "select from" in p.sql.lower()):
            ctx.check([Finding("EXECUTES", "unquoted reserved-word column",
                               "a column named `from`/`to` reached the SQL "
                               "unquoted", [p])])


def suite_role_playing_airports(ctx: SuiteContext) -> None:
    """``flight.from`` and ``flight.to`` are two edges to one dimension.

    Origin and destination must not compile to the same join — the counts
    differ, and neither answer looks wrong on its own.

    Phrased with the schema's own words. "departure airport" → ``from`` is a
    *synonym*, and expanding those is the semantic layer's job, not the
    planner's — the same capability line drawn for ``acctbal`` and ``mktsegment``.
    """
    origin = ctx.probe("how many flights per from airport")
    destination = ctx.probe("how many flights per to airport")
    ctx.check(inv.check_executes([origin, destination]))
    if origin.ok and destination.ok and origin.sql and destination.sql:
        if origin.sql == destination.sql:
            ctx.check([Finding("GOLDEN", "role-playing dimension collapsed",
                               "the from-airport and to-airport questions "
                               "compiled to one statement", [origin, destination])])
        for probe, key in ((origin, "from"), (destination, "to")):
            if f"`{key}`" not in probe.sql and f'"{key}"' not in probe.sql:
                ctx.check([Finding("GOLDEN", "wrong role column",
                                   f"the {key}-airport question never touched "
                                   f"flight.{key}", [probe])])

    # The semantic phrasing must still run cleanly, even where it under-reads.
    ctx.check(inv.check_executes([
        ctx.probe("how many flights per departure airport"),
        ctx.probe("how many flights per arrival airport")]))


def suite_one_to_one(ctx: SuiteContext) -> None:
    """``passengerdetails`` is 1:1 under ``passenger``: joining it may not change
    a count, and grouping by one of its columns must still sum back."""
    total = ctx.probe("how many passengers")
    with_details = ctx.probe("how many passengers with their country")
    ctx.check(inv.check_executes([total, with_details]))

    by_country = ctx.probe("how many passengers per country")
    ctx.check(inv.check_executes([by_country]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_country))


def suite_fanout(ctx: SuiteContext) -> None:
    """The conservation checks, on the largest fact in the suite."""
    total = ctx.probe("total booking price")
    by_seat = ctx.probe("total booking price per seat")
    ctx.check(inv.check_executes([total, by_seat]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_seat))

    count_total = ctx.probe("how many airplanes")
    by_airline = ctx.probe("how many airplanes per airline")
    ctx.check(inv.check_executes([count_total, by_airline]))
    ctx.check(inv.check_breakdown_sums_to_total(count_total, by_airline))


def suite_joins(ctx: SuiteContext) -> None:
    """Dimensions one and two hops out: airplane → airline, airport → geo."""
    probes = [ctx.probe(q) for q in (
        "how many airplanes per airline",
        "how many airports per country",
        "list airlines with their base airport",
        "how many bookings per flight")]
    ctx.check(inv.check_executes(probes))

    per_country = ctx.probe("how many airports per country")
    countries = ctx.truth("SELECT COUNT(DISTINCT country) FROM airport_geo")
    if per_country.ok and len(per_country.rows) not in (countries, countries + 1):
        ctx.check([Finding("GOLDEN", "wrong join grain",
                           f"airports per country returned {len(per_country.rows)} "
                           f"rows, expected ~{countries}", [per_country])])


def suite_filters(ctx: SuiteContext) -> None:
    unfiltered = ctx.probe("how many employees")
    for question in ("how many employees in Berlin",
                     "how many employees with sex M"):
        filtered = ctx.probe(question)
        ctx.check(inv.check_executes([filtered]))
        ctx.check(inv.check_filter_narrows(unfiltered, filtered))


def suite_time_series(ctx: SuiteContext) -> None:
    """``weatherdata`` is a real time series with a column literally named
    ``time`` and one named ``weather``."""
    probes = [ctx.probe(q) for q in (
        "how many weather records",
        "average temperature",
        "how many weather records per station")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_golden(ctx.probe("how many weather records"),
                               ctx.truth("SELECT COUNT(*) FROM weatherdata")))


def suite_ranking(ctx: SuiteContext) -> None:
    for question, cap in (("top 5 airlines by number of airplanes", 5),
                          ("top 3 countries by number of airports", 3)):
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        if p.ok and len(p.rows) > cap:
            ctx.check([Finding("GOLDEN", "ranking ignored the grain or the cap",
                               f"{question!r} returned {len(p.rows)} rows", [p])])


def suite_paraphrase(ctx: SuiteContext) -> None:
    ctx.check(inv.check_paraphrases_agree([
        ctx.probe("how many bookings"),
        ctx.probe("count the bookings"),
        ctx.probe("total number of bookings")]))


def suite_determinism(ctx: SuiteContext) -> None:
    for question in ("total booking price", "how many airplanes per airline",
                     "average airplane capacity"):
        first, second = ctx.probe(question), ctx.probe(question)
        if first.sql != second.sql:
            ctx.check([Finding("INVARIANT", "non-deterministic plan",
                               "the same question compiled to two different "
                               "statements", [first, second])])


def suite_confidence(ctx: SuiteContext) -> None:
    """Columns repeated across tables — ``name`` on airport/airport_geo,
    ``city``/``country`` on employee, passengerdetails and airport_geo,
    ``firstname``/``lastname`` on employee and passenger."""
    probes = [ctx.probe(q) for q in ("list names", "how many people in Berlin")]
    ctx.check(inv.check_not_confident([p for p in probes if p.ok]))


def suite_stress(ctx: SuiteContext) -> None:
    probes = [
        ctx.probe("bookings'; DROP TABLE flight; --"),
        ctx.probe("SELECT * FROM booking"),
        ctx.probe("how many " + "delayed " * 60 + "flights"),
        ctx.probe("航班数量"),
        ctx.probe("bookings where price > 1000000 and price < 1"),
        ctx.probe("total"),
    ]
    for p in probes:
        if p.sql and any(kw in p.sql.upper().split()
                         for kw in ("DROP", "DELETE", "UPDATE", "INSERT",
                                    "TRUNCATE", "ALTER")):
            ctx.check([Finding("EXECUTES", "generated a write",
                               "a read-only question compiled to a write", [p])])


SUITES: dict[str, Callable[[SuiteContext], None]] = {
    "basics": suite_basics,
    "measures": suite_measures,
    "reserved_word_columns": suite_reserved_word_columns,
    "role_playing_airports": suite_role_playing_airports,
    "one_to_one": suite_one_to_one,
    "fanout": suite_fanout,
    "joins": suite_joins,
    "filters": suite_filters,
    "time_series": suite_time_series,
    "ranking": suite_ranking,
    "paraphrase": suite_paraphrase,
    "determinism": suite_determinism,
    "confidence": suite_confidence,
    "stress": suite_stress,
}
