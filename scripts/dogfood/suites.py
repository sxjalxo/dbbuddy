"""Probe suites, ordered by increasing complexity and stress.

Each suite asks the engine questions and asserts *relationships between answers*
rather than expected values, so adding a database costs nothing. See
``invariants`` for why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.invariants import Finding, Probe, Report


@dataclass
class SuiteContext:
    ask: Callable[[str], Probe]     # natural language -> Probe
    sql: Callable[[str], list]      # raw SQL -> rows (ground truth)
    report: Report
    verbose: bool = False

    def probe(self, question: str) -> Probe:
        p = self.ask(question)
        self.report.probes_run += 1
        if self.verbose:
            status = "ok " if p.ok else "ERR"
            print(f"  [{status}] {question}")
            print(f"        {p.sql or p.error}")
        return p

    def check(self, findings: list[Finding]) -> None:
        self.report.add(findings)

    def truth(self, statement: str):
        """A scalar computed by hand-written SQL — the ground truth side."""
        rows = self.sql(statement)
        return list(rows[0].values())[0] if rows else None


def crash_finding(suite: str, exc: Exception) -> Finding:
    return Finding("EXECUTES", f"suite {suite} crashed",
                   f"{type(exc).__name__}: {exc}")


# ── 1. Single table, no joins ────────────────────────────────────────────────

def suite_basics(ctx: SuiteContext) -> None:
    """The floor: if these fail nothing above them means anything."""
    probes = [
        ctx.probe("how many customers"),
        ctx.probe("how many orders"),
        ctx.probe("how many products"),
        ctx.probe("list customers"),
        ctx.probe("show all regions"),
    ]
    ctx.check(inv.check_executes(probes))

    # Ground truth: a COUNT has one right answer and it is cheap to state.
    count_customers = ctx.probe("how many customers")
    ctx.check(inv.check_golden(count_customers, ctx.truth("SELECT COUNT(*) FROM customers")))
    count_orders = ctx.probe("how many orders")
    ctx.check(inv.check_golden(count_orders, ctx.truth("SELECT COUNT(*) FROM orders")))


# ── 2. Filters ───────────────────────────────────────────────────────────────

def suite_filters(ctx: SuiteContext) -> None:
    unfiltered = ctx.probe("how many orders")
    for question in ("how many orders with status shipped",
                     "how many orders where status is cancelled",
                     "count orders with freight over 100"):
        filtered = ctx.probe(question)
        ctx.check(inv.check_executes([filtered]))
        ctx.check(inv.check_filter_narrows(unfiltered, filtered))

    shipped = ctx.probe("how many orders with status shipped")
    ctx.check(inv.check_golden(
        shipped, ctx.truth("SELECT COUNT(*) FROM orders WHERE status = 'shipped'")))


# ── 3. Paraphrase stability ──────────────────────────────────────────────────

def suite_paraphrase(ctx: SuiteContext) -> None:
    """The engine is deterministic, so a wording change that moves the number
    means the *question* was parsed differently."""
    for group in (
        ["how many customers", "count customers", "total number of customers"],
        ["how many orders with status shipped",
         "count orders where status is shipped",
         "number of shipped orders"],
        ["how many products", "count products", "total products"],
    ):
        probes = [ctx.probe(q) for q in group]
        ctx.check(inv.check_executes(probes))
        ctx.check(inv.check_paraphrases_agree(probes))


# ── 4. Join fan-out — the one that matters ───────────────────────────────────

def suite_fanout(ctx: SuiteContext) -> None:
    """A measure aggregated with and without a grouping that changes the join.

    A plan that joins a 1:N table before aggregating multiplies the measure by
    the child-row count. The result is plausible — revenue 3.7x too high reads as
    a good quarter — so only the disagreement between two paths exposes it.
    """
    total = ctx.probe("total freight")
    by_status = ctx.probe("total freight by status")
    ctx.check(inv.check_executes([total, by_status]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_status))

    total_qty = ctx.probe("total quantity")
    qty_by_order = ctx.probe("total quantity by order")
    ctx.check(inv.check_executes([total_qty, qty_by_order]))
    ctx.check(inv.check_breakdown_sums_to_total(total_qty, qty_by_order))

    # Ground truth for a measure that lives on the child table.
    ctx.check(inv.check_golden(
        total_qty, ctx.truth("SELECT SUM(quantity) FROM order_items")))
    ctx.check(inv.check_golden(
        total, ctx.truth("SELECT SUM(freight) FROM orders")))


# ── 5. Counting across a 1:N boundary ────────────────────────────────────────

def suite_count_vs_join(ctx: SuiteContext) -> None:
    """``how many orders`` must not change because a join was added."""
    plain = ctx.probe("how many orders")
    joined = ctx.probe("how many orders by customer segment")
    ctx.check(inv.check_executes([plain, joined]))
    ctx.check(inv.check_breakdown_sums_to_total(plain, joined))


# ── 6. NULL handling ─────────────────────────────────────────────────────────

def suite_nulls(ctx: SuiteContext) -> None:
    """COUNT(*), COUNT(col) and SUM(col) treat NULL differently, and generated
    SQL routinely picks the wrong one. ``customers.credit_limit`` and
    ``orders.sales_rep_id`` are deliberately sparse."""
    probes = [
        ctx.probe("total credit limit"),
        ctx.probe("average credit limit"),
        ctx.probe("how many customers have a credit limit"),
    ]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_golden(
        probes[0], ctx.truth("SELECT SUM(credit_limit) FROM customers")))


# ── 7. Aggregation shapes ────────────────────────────────────────────────────

def suite_aggregates(ctx: SuiteContext) -> None:
    # `unit_price` exists on BOTH `products` and `order_items`, so a bare "unit
    # price" is genuinely ambiguous and either table is a defensible reading.
    # These probes exist to check MAX/MIN/AVG/SUM, so they name the table; the
    # ambiguity itself is a confidence-scoring question (an ambiguous resolution
    # should not be reported as high confidence) and is probed separately below.
    checks = [
        ("highest product unit price", "SELECT MAX(unit_price) FROM products"),
        ("lowest product unit price", "SELECT MIN(unit_price) FROM products"),
        ("average product unit price", "SELECT AVG(unit_price) FROM products"),
        ("total salary", "SELECT SUM(salary) FROM employees"),
    ]
    for question, truth_sql in checks:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


# ── 8. Grouping + ordering + limit ───────────────────────────────────────────

def suite_topn(ctx: SuiteContext) -> None:
    probes = [
        ctx.probe("top 5 products by unit price"),
        ctx.probe("orders by status"),
        ctx.probe("customers by segment"),
        ctx.probe("employees by department"),
    ]
    ctx.check(inv.check_executes(probes))

    # A grouped count over a single table must total the plain count.
    total_customers = ctx.probe("how many customers")
    by_segment = ctx.probe("how many customers by segment")
    ctx.check(inv.check_executes([by_segment]))
    ctx.check(inv.check_breakdown_sums_to_total(total_customers, by_segment))


# ── 9. Hostile join shapes ───────────────────────────────────────────────────

def suite_hard_joins(ctx: SuiteContext) -> None:
    """Shapes name-based inference cannot resolve.

    ``orders.placed_by_id -> customers`` (FK name unrelated to the table),
    two FKs to ``addresses``, a self-referencing ``employees.manager_id``, and a
    junction table. All are declared FKs, so the catalog-driven graph should
    handle them and the naming heuristic should not be consulted at all.
    """
    probes = [
        ctx.probe("orders by customer segment"),
        ctx.probe("how many orders per sales rep"),
        ctx.probe("total quantity by product category"),
        ctx.probe("customers by region"),
    ]
    ctx.check(inv.check_executes(probes))


# ── 10. Stress: long questions, odd tokens, adversarial input ────────────────

def suite_stress(ctx: SuiteContext) -> None:
    """Nothing here should crash or produce a write. Correctness is secondary —
    the bar is that a strange question fails cleanly."""
    probes = [
        ctx.probe("how many orders with status shipped or delivered placed by "
                  "enterprise customers in the north region last year"),
        ctx.probe("total"),
        ctx.probe("customers'; DROP TABLE orders; --"),
        ctx.probe("how many " + "very " * 60 + "old orders"),
        ctx.probe("SELECT * FROM orders"),
        ctx.probe("списка клиентов"),
        ctx.probe("orders where freight > 100 and freight < 50"),
    ]
    for p in probes:
        if p.sql and any(kw in p.sql.upper().split()
                         for kw in ("DROP", "DELETE", "UPDATE", "INSERT", "TRUNCATE", "ALTER")):
            ctx.check([Finding("EXECUTES", "generated a write",
                               "a read-only question compiled to a write statement", [p])])
    ctx.report.probes_run += 0  # probes already counted


def suite_confidence(ctx: SuiteContext) -> None:
    """Does the engine know when it might be wrong?

    Every question below names a column that lives on two tables — `unit_price`
    on `products` and `order_items`, `full_name` on `employees` and `person`, so
    no single reading is *the* answer. Either resolution is accepted; what is not
    accepted is answering with high confidence and no ambiguity recorded.
    """
    probes = [ctx.probe(q) for q in (
        "average unit price",
        "highest unit price",
        "list full name",
    )]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_not_confident(probes))


SUITES: dict[str, Callable[[SuiteContext], None]] = {
    "basics": suite_basics,
    "filters": suite_filters,
    "paraphrase": suite_paraphrase,
    "fanout": suite_fanout,
    "count_vs_join": suite_count_vs_join,
    "nulls": suite_nulls,
    "aggregates": suite_aggregates,
    "topn": suite_topn,
    "hard_joins": suite_hard_joins,
    "stress": suite_stress,
    "confidence": suite_confidence,
}
