"""Probe suites for TPC-DS — the decision-support / BI shape.

What this dataset adds over the other six is not size, it is *ambiguity that is
structural rather than lexical*:

* the same dimension reached through **two different foreign keys** from one fact
  (``ws_sold_date_sk`` vs ``ws_ship_date_sk``), so "which table" is not enough to
  identify a join;
* **three parallel sales channels** whose measures share a name
  (``ss_ext_sales_price`` / ``cs_ext_sales_price`` / ``ws_ext_sales_price``), so a
  bare measure question has three defensible answers;
* a **surrogate key beside a business key** on every dimension (``c_customer_sk``
  vs ``c_customer_id``);
* **nullable foreign keys** in the facts, so the join *type* decides whether a
  total survives its own breakdown.

Golden values come from the loaded database (``ctx.truth``), so they cannot drift.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.invariants import Finding
from scripts.dogfood.suites import SuiteContext


def suite_basics(ctx: SuiteContext) -> None:
    """Counts across dimensions and facts. Every table is prefixed and every key
    ends in ``_sk``; a count that lands on the wrong table is invisible in the
    result, so these are golden."""
    probes = [ctx.probe(q) for q in (
        "how many customers", "how many items", "how many stores",
        "how many promotions", "how many warehouses", "list reasons")]
    ctx.check(inv.check_executes(probes))
    for question, table in (("how many customers", "customer"),
                            ("how many items", "item"),
                            ("how many stores", "store"),
                            ("how many promotions", "promotion"),
                            ("how many warehouses", "warehouse")):
        ctx.check(inv.check_golden(ctx.probe(question),
                                   ctx.truth(f"SELECT COUNT(*) FROM {table}")))


def suite_multiword_tables(ctx: SuiteContext) -> None:
    """Underscore-joined table names spoken as separate words, including the
    ones that share a prefix with a shorter table (``customer`` vs
    ``customer_address`` vs ``customer_demographics``)."""
    for question, table in (
            ("how many customer addresses", "customer_address"),
            ("how many customer demographics", "customer_demographics"),
            ("how many household demographics", "household_demographics"),
            ("how many store sales", "store_sales"),
            ("how many web sales", "web_sales"),
            ("how many catalog sales", "catalog_sales"),
            ("how many store returns", "store_returns")):
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(f"SELECT COUNT(*) FROM {table}")))


def suite_measures(ctx: SuiteContext) -> None:
    """A measure named in words must bind to the right column of the right
    channel. ``ss_``/``cs_``/``ws_`` share measure names, so naming the channel
    is what disambiguates."""
    cases = [
        ("total store sales quantity", "SELECT SUM(ss_quantity) FROM store_sales"),
        ("total store sales net profit",
         "SELECT SUM(ss_net_profit) FROM store_sales"),
        ("average web sales list price",
         "SELECT AVG(ws_list_price) FROM web_sales"),
        ("highest item current price",
         "SELECT MAX(i_current_price) FROM item"),
    ]
    for question, truth_sql in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


def suite_surrogate_keys(ctx: SuiteContext) -> None:
    """``_sk`` is a surrogate key and ``_id`` a business key — neither is ever a
    measure. Summing one produces a number that looks like money."""
    for question in ("total store sales quantity", "total store sales net profit",
                     "average web sales list price", "total item current price"):
        p = ctx.probe(question)
        if not p.sql:
            continue
        import re
        for expr in re.findall(r"(?:SUM|AVG)\(([^)]+)\)", p.sql, re.I):
            column = expr.split(".")[-1].strip('"` ').lower()
            if column.endswith("_sk") or column.endswith("_id"):
                ctx.check([Finding("GOLDEN", "aggregated a key column",
                                   f"aggregate over {expr.strip()} — a surrogate "
                                   f"or business key is not a measure", [p])])


def suite_grouping(ctx: SuiteContext) -> None:
    """Group by a dimension attribute at the right grain."""
    cases = [
        ("how many items per category",
         "SELECT COUNT(DISTINCT i_category) FROM item"),
        ("how many customer demographics per marital status",
         "SELECT COUNT(DISTINCT cd_marital_status) FROM customer_demographics"),
        ("how many stores per state",
         "SELECT COUNT(DISTINCT s_state) FROM store"),
    ]
    for question, truth_sql in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        expected = ctx.truth(truth_sql)
        if p.ok and len(p.rows) not in (expected, expected + 1):
            ctx.check([Finding("GOLDEN", "wrong grouping grain",
                               f"{question!r} returned {len(p.rows)} rows, "
                               f"expected ~{expected}", [p])])


def suite_fanout(ctx: SuiteContext) -> None:
    """Conservation across a grouping on the fact's own columns — no join, so
    nothing can fan out and nothing can be dropped. If these disagree, the
    grouped plan itself is losing or duplicating rows."""
    total = ctx.probe("total store sales net profit")
    by_quantity = ctx.probe("total store sales net profit by store sales quantity")
    ctx.check(inv.check_executes([total, by_quantity]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_quantity))

    count_total = ctx.probe("how many items")
    by_category = ctx.probe("how many items per category")
    ctx.check(inv.check_executes([count_total, by_category]))
    ctx.check(inv.check_breakdown_sums_to_total(count_total, by_category))


def suite_snowflake(ctx: SuiteContext) -> None:
    """Dimensions reached through another dimension: customer →
    customer_address / customer_demographics, and household_demographics →
    income_band. The measure and the grain sit two joins apart."""
    probes = [ctx.probe(q) for q in (
        "how many customers per state",
        "how many customers per education status",
        "how many household demographics per income band",
        "list customers with their city")]
    ctx.check(inv.check_executes(probes))


def suite_role_playing_dates(ctx: SuiteContext) -> None:
    """The failure mode this dataset exists for.

    ``web_sales`` reaches ``date_dim`` through ``ws_sold_date_sk`` *and*
    ``ws_ship_date_sk``, and ``customer`` through ``ws_bill_customer_sk`` *and*
    ``ws_ship_customer_sk``. Two questions that name different roles must not
    compile to the same join: an answer about shipping dates computed from sale
    dates is wrong in a way no validator and no invariant can see.
    """
    sold = ctx.probe("total web sales net profit by sold date")
    shipped = ctx.probe("total web sales net profit by ship date")
    ctx.check(inv.check_executes([sold, shipped]))
    if sold.ok and shipped.ok and sold.sql and shipped.sql:
        if sold.sql == shipped.sql:
            ctx.check([Finding("GOLDEN", "role-playing dimension collapsed",
                               "the sold-date and ship-date questions compiled to "
                               "one statement — the two roles are different joins",
                               [sold, shipped])])

    bill = ctx.probe("how many web sales per bill customer")
    ship = ctx.probe("how many web sales per ship customer")
    ctx.check(inv.check_executes([bill, ship]))
    if bill.ok and ship.ok and bill.sql and ship.sql and bill.sql == ship.sql:
        ctx.check([Finding("GOLDEN", "role-playing dimension collapsed",
                           "bill-customer and ship-customer compiled to one "
                           "statement", [bill, ship])])


def suite_parallel_facts(ctx: SuiteContext) -> None:
    """The same measure name exists in three channels. Naming one must select it;
    naming none must not read as certain."""
    for question, truth_sql in (
            ("total store sales ext sales price",
             "SELECT SUM(ss_ext_sales_price) FROM store_sales"),
            ("total web sales ext sales price",
             "SELECT SUM(ws_ext_sales_price) FROM web_sales"),
            ("total catalog sales ext sales price",
             "SELECT SUM(cs_ext_sales_price) FROM catalog_sales")):
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))

    ambiguous = [ctx.probe(q) for q in ("total ext sales price",
                                        "total net profit")]
    ctx.check(inv.check_not_confident([p for p in ambiguous if p.ok]))


def suite_filters(ctx: SuiteContext) -> None:
    unfiltered = ctx.probe("how many items")
    for question in ("how many items with category Books",
                     "how many items with color azure"):
        filtered = ctx.probe(question)
        ctx.check(inv.check_executes([filtered]))
        ctx.check(inv.check_filter_narrows(unfiltered, filtered))


def suite_ranking(ctx: SuiteContext) -> None:
    """Reporting shape: ranked breakdown with a cap. "by" introduces the metric
    here, not the dimension."""
    for question, cap in (("top 5 categories by number of items", 5),
                          ("top 3 states by number of stores", 3)):
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        if p.ok and len(p.rows) > cap:
            ctx.check([Finding("GOLDEN", "ranking ignored the grain or the cap",
                               f"{question!r} returned {len(p.rows)} rows",
                               [p])])


def suite_paraphrase(ctx: SuiteContext) -> None:
    ctx.check(inv.check_paraphrases_agree([
        ctx.probe("how many store sales"),
        ctx.probe("count the store sales"),
        ctx.probe("total number of store sales")]))


def suite_determinism(ctx: SuiteContext) -> None:
    for question in ("total store sales net profit", "how many items per category",
                     "total web sales ext sales price"):
        first, second = ctx.probe(question), ctx.probe(question)
        if first.sql != second.sql:
            ctx.check([Finding("INVARIANT", "non-deterministic plan",
                               "the same question compiled to two different "
                               "statements", [first, second])])


def suite_stress(ctx: SuiteContext) -> None:
    probes = [
        ctx.probe("items'; DROP TABLE store_sales; --"),
        ctx.probe("SELECT * FROM store_sales"),
        ctx.probe("how many " + "returned " * 60 + "items"),
        ctx.probe("店铺销售数量"),
        ctx.probe("items where i_current_price > 1000000 and i_current_price < 1"),
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
    "multiword_tables": suite_multiword_tables,
    "measures": suite_measures,
    "surrogate_keys": suite_surrogate_keys,
    "grouping": suite_grouping,
    "fanout": suite_fanout,
    "snowflake": suite_snowflake,
    "role_playing_dates": suite_role_playing_dates,
    "parallel_facts": suite_parallel_facts,
    "filters": suite_filters,
    "ranking": suite_ranking,
    "paraphrase": suite_paraphrase,
    "determinism": suite_determinism,
    "stress": suite_stress,
}
