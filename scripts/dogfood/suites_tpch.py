"""Probe suites for TPC-H — the analytics/warehouse shape.

Two things are under test here that the other five datasets cannot reach:

**Table-prefixed column names.** Every column is ``<initial>_<word>``, and the
word is usually a *concatenation* (``l_extendedprice``, ``c_mktsegment``,
``o_orderdate``). A user says "extended price"; word-level matching sees
``[l, extendedprice]`` and finds no overlap. This is the same class of bug
AdventureWorks' camelCase exposed, in the one convention camelCase splitting
cannot help with.

**Key columns named ``key``.** ``l_orderkey``, ``c_custkey``, ``ps_partkey`` —
nothing in the schema is named ``id``, so an identifier check that only knows
``id``/``XxxID`` reads every foreign key as a measure and sums it.

Above the naming, the shape: a 300k-row fact (``lineitem``) joined to ``orders``,
``part`` and ``supplier``, with ``nation``/``region`` three hops away. Every
aggregate is a join aggregate, so fan-out has somewhere to hide — and the
generator makes ``o_totalprice`` the true sum of its lines, which turns that
identity into a free invariant.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.invariants import Finding
from scripts.dogfood.suites import SuiteContext


def suite_basics(ctx: SuiteContext) -> None:
    """Counts on every table, including the two concatenated names."""
    probes = [ctx.probe(q) for q in (
        "how many customers", "how many orders", "how many parts",
        "how many suppliers", "how many nations", "how many regions",
        "list regions")]
    ctx.check(inv.check_executes(probes))
    for question, table in (("how many customers", "customer"),
                            ("how many orders", "orders"),
                            ("how many parts", "part"),
                            ("how many suppliers", "supplier"),
                            ("how many nations", "nation")):
        ctx.check(inv.check_golden(ctx.probe(question),
                                   ctx.truth(f"SELECT COUNT(*) FROM {table}")))


def suite_concatenated_tables(ctx: SuiteContext) -> None:
    """``lineitem`` and ``partsupp`` have no separator between their words. Asked
    as two words they must still resolve, and must not fall back to a shorter
    table that shares a prefix (``part``, ``supplier``)."""
    for question, table in (("how many line items", "lineitem"),
                            ("how many lineitems", "lineitem")):
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(f"SELECT COUNT(*) FROM {table}")))
    # "part suppliers" is not ``partsupp`` — expanding that abbreviation needs a
    # synonym layer, which is a capability, not a defect. It must still run.
    ctx.check(inv.check_executes([ctx.probe("how many partsupp rows")]))


def suite_prefixed_measures(ctx: SuiteContext) -> None:
    """A measure spoken in words must bind to the prefixed, concatenated column
    that holds it — and never to a ``*key`` column, which is a key, not a
    measure."""
    cases = [
        ("total extended price", "SELECT SUM(l_extendedprice) FROM lineitem"),
        ("average quantity", "SELECT AVG(l_quantity) FROM lineitem"),
        ("highest retail price", "SELECT MAX(p_retailprice) FROM part"),
        ("average supply cost", "SELECT AVG(ps_supplycost) FROM partsupp"),
        ("total order price", "SELECT SUM(o_totalprice) FROM orders"),
    ]
    for question, truth_sql in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


def suite_keys_are_not_measures(ctx: SuiteContext) -> None:
    """No ``*key`` column may be aggregated as a measure. Summing ``l_orderkey``
    produces a large, plausible, entirely meaningless number — the failure mode
    that made "total sales" into ``SUM(SalesOrderID)`` on AdventureWorks."""
    for question in ("total extended price", "total order price",
                     "total quantity", "average supply cost",
                     # An unmatchable measure phrase must not fall back to a key:
                     # SUM over a surrogate key returns a large plausible number
                     # that no invariant can catch, because every phrasing of the
                     # question sums the same meaningless column.
                     "total account balance of customers",
                     "total account balance of customers per nation",
                     "average market segment of customers"):
        p = ctx.probe(question)
        if not p.sql:
            continue
        lowered = p.sql.lower()
        for agg in ("sum(", "avg("):
            start = 0
            while True:
                i = lowered.find(agg, start)
                if i < 0:
                    break
                inner = lowered[i + len(agg):lowered.find(")", i)]
                start = i + 1
                if inner.split(".")[-1].strip('"` ').endswith("key"):
                    ctx.check([Finding(
                        "GOLDEN", "aggregated a key column",
                        f"{agg[:-1].upper()} over {inner.strip()} — a key is not a "
                        f"measure", [p])])


def suite_grouping(ctx: SuiteContext) -> None:
    """Group by a real dimension at the right grain — the coded single-character
    columns (``l_returnflag``, ``o_orderstatus``) and the run-together
    ``o_orderpriority``/``l_shipmode``."""
    cases = [
        ("how many orders per order status",
         "SELECT COUNT(DISTINCT o_orderstatus) FROM orders"),
        ("how many orders per order priority",
         "SELECT COUNT(DISTINCT o_orderpriority) FROM orders"),
        ("how many line items per ship mode",
         "SELECT COUNT(DISTINCT l_shipmode) FROM lineitem"),
    ]
    for question, truth_sql in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        expected = ctx.truth(truth_sql)
        if p.ok and len(p.rows) not in (expected, expected + 1):
            ctx.check([Finding("GOLDEN", "wrong grouping grain",
                               f"{question!r} returned {len(p.rows)} rows, "
                               f"expected ~{expected} groups", [p])])


def suite_fanout(ctx: SuiteContext) -> None:
    """The invariant this dataset exists for.

    ``lineitem`` is 4x ``orders``, so any plan that joins the two before summing
    an order-level measure inflates it ~4x — and 4x revenue reads as a good year,
    not as a bug. Conservation across a grouping catches it without an oracle.
    """
    total = ctx.probe("total order price")
    by_status = ctx.probe("total order price by order status")
    ctx.check(inv.check_executes([total, by_status]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_status))

    line_total = ctx.probe("total extended price")
    by_flag = ctx.probe("total extended price by return flag")
    ctx.check(inv.check_executes([line_total, by_flag]))
    ctx.check(inv.check_breakdown_sums_to_total(line_total, by_flag))

    count_total = ctx.probe("how many line items")
    count_by_mode = ctx.probe("how many line items per ship mode")
    ctx.check(inv.check_executes([count_total, count_by_mode]))
    ctx.check(inv.check_breakdown_sums_to_total(count_total, count_by_mode))


def suite_joins(ctx: SuiteContext) -> None:
    """Dimensions reached only through a join — ``nation`` is one hop from
    customer/supplier, ``region`` two, and the fact table three."""
    probes = [ctx.probe(q) for q in (
        "how many customers per nation",
        "how many suppliers per nation",
        "list customers with their nation",
        "how many orders per market segment")]
    ctx.check(inv.check_executes(probes))

    per_nation = ctx.probe("how many customers per nation")
    n_nations = ctx.truth("SELECT COUNT(DISTINCT c_nationkey) FROM customer")
    if per_nation.ok and len(per_nation.rows) not in (n_nations, n_nations + 1):
        ctx.check([Finding("GOLDEN", "wrong join grain",
                           f"customers per nation returned {len(per_nation.rows)} "
                           f"rows, expected {n_nations}", [per_nation])])
    total_customers = ctx.probe("how many customers")
    ctx.check(inv.check_breakdown_sums_to_total(total_customers, per_nation))


def suite_multi_hop(ctx: SuiteContext) -> None:
    """Group a fact by a dimension that is two or three joins away.

    ``region`` is reachable only as customer → nation → region, and the money
    columns live one further hop out on ``lineitem``. Conservation is the check:
    however the planner routes the join, the grouped rows must still sum to the
    ungrouped total. A route that passes through a 1:N table on the way to the
    dimension multiplies the measure, and nothing about the resulting number
    looks wrong.
    """
    pairs = [
        ("how many customers", "how many customers per region"),
        ("how many orders", "how many orders per nation"),
        ("how many suppliers", "how many suppliers per region"),
        ("total account balance of customers",
         "total account balance of customers per nation"),
    ]
    for total_q, grouped_q in pairs:
        total, grouped = ctx.probe(total_q), ctx.probe(grouped_q)
        ctx.check(inv.check_executes([total, grouped]))
        ctx.check(inv.check_breakdown_sums_to_total(total, grouped))


def suite_ranking(ctx: SuiteContext) -> None:
    """The reporting shape: a ranked breakdown with a row cap.

    "top 5 X by Y" must return five rows at the X grain, not five raw rows of
    the fact table and not one grand total — the wrong shape is what makes a
    ranked answer wrong, long before the numbers are.
    """
    cases = [
        ("top 5 nations by number of customers", 5),
        ("top 3 ship modes by total extended price", 3),
        ("top 10 order priorities by number of orders", 5),  # only 5 exist
    ]
    for question, cap in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        if p.ok and len(p.rows) > cap:
            ctx.check([Finding("GOLDEN", "ranking ignored the grain or the cap",
                               f"{question!r} returned {len(p.rows)} rows, at most "
                               f"{cap} were possible", [p])])


def suite_fact_multi_hop(ctx: SuiteContext) -> None:
    """A measure on the 300k-row fact, grouped by a dimension two joins away.

    ``lineitem`` → ``supplier`` → ``nation`` is the shape of every real TPC-H
    query, and the one where a wrong join route silently multiplies the money.
    """
    total = ctx.probe("total extended price")
    for grouped_q in ("total extended price per nation",
                      "total extended price per ship mode",
                      "total quantity per return flag"):
        grouped = ctx.probe(grouped_q)
        ctx.check(inv.check_executes([grouped]))
        base = ctx.probe("total quantity") if grouped_q.startswith("total quantity") else total
        ctx.check(inv.check_breakdown_sums_to_total(base, grouped))


def suite_filters(ctx: SuiteContext) -> None:
    """Filters on coded and prefixed dimension columns must narrow, never widen."""
    unfiltered = ctx.probe("how many orders")
    for question in ("how many orders with order status F",
                     "how many orders with order priority 1-URGENT"):
        filtered = ctx.probe(question)
        ctx.check(inv.check_executes([filtered]))
        ctx.check(inv.check_filter_narrows(unfiltered, filtered))

    all_customers = ctx.probe("how many customers")
    segment = ctx.probe("how many customers in the BUILDING market segment")
    ctx.check(inv.check_executes([segment]))
    ctx.check(inv.check_filter_narrows(all_customers, segment))


def suite_paraphrase(ctx: SuiteContext) -> None:
    """Two phrasings of one warehouse question must return one number."""
    ctx.check(inv.check_paraphrases_agree([
        ctx.probe("how many line items"),
        ctx.probe("count the line items"),
        ctx.probe("total number of line items")]))
    ctx.check(inv.check_paraphrases_agree([
        ctx.probe("total extended price"),
        ctx.probe("sum of extended price")]))


def suite_determinism(ctx: SuiteContext) -> None:
    """The same question twice must compile to the same SQL. Ambiguous measures
    on a prefixed schema (``comment`` on all eight tables, ``*key`` on most) are
    where set-iteration order used to leak into the answer."""
    for question in ("total extended price", "how many line items per ship mode",
                     "average supply cost"):
        first, second = ctx.probe(question), ctx.probe(question)
        if first.sql != second.sql:
            ctx.check([Finding("INVARIANT", "non-deterministic plan",
                               "the same question compiled to two different "
                               "statements", [first, second])])


def suite_unmatched_measure(ctx: SuiteContext) -> None:
    """A measure the schema abbreviates (``c_acctbal``, ``c_mktsegment``) cannot
    be matched from the words a user says, and expanding abbreviations is a
    capability for a later phase — but *falling back to an unrelated numeric
    column and reporting it as certain* is a defect now. The answer to "total
    account balance" must not silently become the sum of some other table's money
    column. Either the aggregate is over a column the query actually names, or
    the result must not read as high confidence.
    """
    from dbbuddy_core.intent_builder import (
        column_match_tokens, expand_query_tokens, tokens_present,
        uniform_column_prefix)

    schema = {t: ctx.column_names(t)
              for t in ("customer", "lineitem", "orders", "part",
                        "partsupp", "supplier")}

    for question in ("total account balance", "average account balance",
                     "total market segment value"):
        p = ctx.probe(question)
        if not p.ok or not p.sql:
            continue  # refusing to answer is a calibrated outcome
        import re as _re
        aggregated = _re.findall(r"(?:SUM|AVG|MAX|MIN)\(([^)]+)\)", p.sql, _re.I)
        named = False
        query_tokens = expand_query_tokens(question)
        for expr in aggregated:
            col = expr.split(".")[-1].strip('"` ')
            table = expr.split(".")[0].strip('"` ') if "." in expr else ""
            prefix = uniform_column_prefix(schema.get(table, []))
            if tokens_present(column_match_tokens(col, prefix), query_tokens):
                named = True
        confidence = str(p.meta.get("confidence") or "").lower()
        if aggregated and not named and confidence == "high" and not p.meta.get("ambiguities"):
            ctx.check([Finding(
                "CONFIDENCE", "confident about an unmatched measure",
                f"aggregated {aggregated} — no part of which the question names — "
                f"and reported confidence={confidence!r}", [p])])


def suite_confidence(ctx: SuiteContext) -> None:
    """``comment`` is on all eight tables and ``*key`` columns are on most: naming
    one bare has several defensible readings and must not read as certain.

    Being *held* rather than answered counts as calibrated — that is the engine
    saying it is unsure, which is the behaviour under test.

    Note "how many part keys" is deliberately *not* here: once the engine stopped
    dragging `lineitem` into it, the question resolves to one table and one
    column, and reporting that confidently is right. A probe that no longer has
    two readings does not test calibration. A prefixed schema is in fact *less*
    lexically ambiguous than a plain one — every column carries its table, so
    "total order price" has exactly one reading. What stays ambiguous is a
    measure word that reaches no column at all ("average cost": there is a
    ``ps_supplycost``, but the question names no table and the word never
    matches it), and a column name that every table repeats."""
    probes = [ctx.probe(q) for q in (
        "list comments",        # ``*_comment`` on all eight tables
        "average cost")]        # nothing named "cost" is reachable from the words
    ctx.check(inv.check_not_confident([p for p in probes if p.ok]))


def suite_stress(ctx: SuiteContext) -> None:
    probes = [
        ctx.probe("lineitem'; DROP TABLE orders; --"),
        ctx.probe("SELECT * FROM lineitem"),
        ctx.probe("how many " + "urgent " * 60 + "orders"),
        ctx.probe("订单数量"),
        ctx.probe("orders where o_totalprice > 1000000 and o_totalprice < 1"),
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
    "concatenated_tables": suite_concatenated_tables,
    "prefixed_measures": suite_prefixed_measures,
    "keys_are_not_measures": suite_keys_are_not_measures,
    "grouping": suite_grouping,
    "fanout": suite_fanout,
    "joins": suite_joins,
    "multi_hop": suite_multi_hop,
    "fact_multi_hop": suite_fact_multi_hop,
    "ranking": suite_ranking,
    "filters": suite_filters,
    "paraphrase": suite_paraphrase,
    "determinism": suite_determinism,
    "unmatched_measure": suite_unmatched_measure,
    "confidence": suite_confidence,
    "stress": suite_stress,
}
