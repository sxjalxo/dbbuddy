"""Probe suites for Microsoft's AdventureWorks OLTP.

Where the employees dataset tests **depth** (one HR domain at ~4M rows), this
tests **width and business vocabulary**: 68 tables across 16 schemas, 91 declared
foreign keys, camelCase `PascalCase` table and `XxxID` key names, and columns
whose names collide across a dozen domains. Golden values are computed from the
loaded database (``ctx.truth``), so they never drift from the data.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.suites import SuiteContext


def suite_basics(ctx: SuiteContext) -> None:
    """Counts across domains — every one is a camelCase table with an ``XxxID``
    key, so a wrong table or an id-as-measure slip shows immediately."""
    probes = [ctx.probe(q) for q in (
        "how many products", "how many customers", "how many vendors",
        "how many sales order headers", "how many sales order details",
        "list departments")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_golden(ctx.probe("how many products"),
                               ctx.truth("SELECT COUNT(*) FROM Product")))
    ctx.check(inv.check_golden(ctx.probe("how many sales order headers"),
                               ctx.truth("SELECT COUNT(*) FROM SalesOrderHeader")))
    ctx.check(inv.check_golden(ctx.probe("how many sales order details"),
                               ctx.truth("SELECT COUNT(*) FROM SalesOrderDetail")))


def suite_camelcase_tables(ctx: SuiteContext) -> None:
    """Multi-word PascalCase tables spoken as separate words must resolve to the
    right table — not a shorter one that shares a prefix (SalesPerson vs
    SalesOrderHeader, Product vs ProductSubcategory)."""
    cases = [
        ("how many sales order headers", "SalesOrderHeader"),
        ("how many product subcategories", "ProductSubcategory"),
        ("how many product categories", "ProductCategory"),
        ("how many sales territories", "SalesTerritory"),
        ("how many special offers", "SpecialOffer"),
    ]
    for question, table in cases:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(f"SELECT COUNT(*) FROM {table}")))


def suite_measures(ctx: SuiteContext) -> None:
    """A measure named by the query must bind to the right *numeric* column — not
    a camelCase id and not a text code that merely contains a measure word
    (SalesOrderNumber, AccountNumber)."""
    checks = [
        ("average list price", "SELECT AVG(ListPrice) FROM Product"),
        ("highest list price", "SELECT MAX(ListPrice) FROM Product"),
        ("total freight", "SELECT SUM(Freight) FROM SalesOrderHeader"),
        ("highest standard cost", "SELECT MAX(StandardCost) FROM Product"),
    ]
    for question, truth_sql in checks:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


def suite_grouping(ctx: SuiteContext) -> None:
    """Grouping on a real dimension, at the right grain."""
    from scripts.dogfood.invariants import Finding
    p = ctx.probe("how many products per color")
    ctx.check(inv.check_executes([p]))
    n_color = ctx.truth("SELECT COUNT(DISTINCT Color) FROM Product WHERE Color IS NOT NULL")
    if p.ok and len(p.rows) not in (n_color, n_color + 1):  # +1 tolerates a NULL bucket
        ctx.check([Finding("GOLDEN", "wrong grouping grain",
                           f"products per color returned {len(p.rows)} rows, "
                           f"expected ~{n_color} colors", [p])])


def suite_fanout(ctx: SuiteContext) -> None:
    """Conservation across a grouping: the per-color product counts must sum back
    to the total product count. Single table, so no join can fan out — what this
    guards is that the grouped plan neither drops nor double-counts rows."""
    from scripts.dogfood.invariants import Finding
    total = ctx.probe("how many products")
    by_color = ctx.probe("how many products per color")
    ctx.check(inv.check_executes([total, by_color]))
    if total.ok and by_color.ok:
        grand = total.scalar()
        # sum the count column across the grouped rows
        summed = 0
        for row in by_color.rows:
            nums = [v for v in row.values() if isinstance(v, (int, float))]
            if nums:
                summed += nums[-1]
        if grand is not None and summed != grand:
            ctx.check([Finding("INVARIANT", "grouped counts do not sum to total",
                               f"per-color counts sum to {summed}, total is {grand}",
                               [total, by_color])])


def suite_domains(ctx: SuiteContext) -> None:
    """Broad width coverage — one question per business domain. The bar is that a
    real ERP question runs cleanly and does not compile to a write; correctness of
    the richer ones is a later, semantic phase."""
    probes = [ctx.probe(q) for q in (
        "how many employees",                       # HR
        "how many purchase order details",          # Purchasing
        "how many work orders",                     # Manufacturing
        "how many product inventory records",       # Inventory
        "how many addresses",                        # Geography
        "how many credit cards",                     # Finance
        "how many stores",                           # Sales/CRM
        "how many bill of materials")]              # Manufacturing / self-ref
    ctx.check(inv.check_executes(probes))


def suite_confidence(ctx: SuiteContext) -> None:
    """Columns that live on many tables (``ModifiedDate`` on nearly all,
    ``BusinessEntityID`` across Person/HR/Sales/Purchasing) must not read as
    high-confidence when named bare."""
    # StandardCost lives on Product and ProductCostHistory; Freight on
    # SalesOrderHeader and PurchaseOrderHeader. Naming neither, the answer must not
    # read as certain — the cross-domain column collisions are exactly what a
    # 68-table schema adds over a single-domain one.
    probes = [ctx.probe(q) for q in (
        "average standard cost",
        "total freight")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_not_confident(probes))


def suite_stress(ctx: SuiteContext) -> None:
    from scripts.dogfood.invariants import Finding
    probes = [
        ctx.probe("products'; DROP TABLE SalesOrderHeader; --"),
        ctx.probe("SELECT * FROM Product"),
        ctx.probe("how many " + "discontinued " * 60 + "products"),
        ctx.probe("产品数量"),
        ctx.probe("products where ListPrice > 1000000 and ListPrice < 1"),
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
    "camelcase_tables": suite_camelcase_tables,
    "measures": suite_measures,
    "grouping": suite_grouping,
    "fanout": suite_fanout,
    "domains": suite_domains,
    "confidence": suite_confidence,
    "stress": suite_stress,
}
