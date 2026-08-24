"""Probe suites for the adversarial-identifier dataset.

The bar here is different from the other two. Several of these questions have no
sensible answer, and that is fine. What must hold is that the engine

* never emits SQL the database rejects,
* never crashes,
* never silently answers a *different* question,
* never produces a write.

So most checks are ``EXECUTES``-tier with a small number of goldens on the cases
that do have one right answer.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.invariants import Finding
from scripts.dogfood.suites import SuiteContext


def _no_write(ctx: SuiteContext, probes) -> None:
    for p in probes:
        if p.sql and any(kw in p.sql.upper().split() for kw in
                         ("DROP", "DELETE", "UPDATE", "INSERT", "TRUNCATE", "ALTER")):
            ctx.check([Finding("EXECUTES", "generated a write",
                               "a read-only question compiled to a write", [p])])


def suite_reserved_words(ctx: SuiteContext) -> None:
    """`order`, `group`, `key`, `value`, `desc`, `index`, `select` are all real
    identifiers here. Unquoted, each is a syntax error somewhere."""
    probes = [ctx.probe(q) for q in (
        "how many order",
        "total value",
        "list order",
        "count line-item",
    )]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)
    ctx.check(inv.check_golden(ctx.probe("how many order"),
                               ctx.truth('SELECT COUNT(*) FROM "order"')))


def suite_quoted_and_spaced(ctx: SuiteContext) -> None:
    """Identifiers containing spaces, hyphens and leading digits."""
    probes = [ctx.probe(q) for q in (
        "total amount", "how many line-item", "total 2024_total")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_case_sensitivity(ctx: SuiteContext) -> None:
    """`Customer` and `CUSTOMER_LOG` coexist; `Customer` is also a *column* on
    `Customer`. A case-insensitive match alone cannot separate them."""
    probes = [ctx.probe(q) for q in (
        "how many Customer", "how many CUSTOMER_LOG", "list Customer")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_near_identical_columns(ctx: SuiteContext) -> None:
    """`user_id`, `userid` and `UserID` on one table defeat normalize-and-compare:
    all three collapse to the same key, so a repair that picks 'the unique match'
    must find three and decline rather than guess."""
    probes = [ctx.probe(q) for q in (
        "list user_id", "count userid", "Customer by user id")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_non_ascii(ctx: SuiteContext) -> None:
    """Spanish and Japanese identifiers, including an accented column name."""
    probes = [ctx.probe(q) for q in (
        "how many cliente", "total précio", "list 顧客", "cliente by nombre")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)
    ctx.check(inv.check_golden(ctx.probe("how many cliente"),
                               ctx.truth('SELECT COUNT(*) FROM "cliente"')))


def suite_long_identifier(ctx: SuiteContext) -> None:
    """A 64-character column name — the MySQL limit, and long enough that any
    truncation shows up as a missing column rather than a subtle mismatch."""
    probes = [ctx.probe(q) for q in (
        "total customer lifetime value including projected renewals and credits",
        "average customer lifetime value including projected renewals and credits")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_shadowed_names(ctx: SuiteContext) -> None:
    """`Customer.Customer` (column named after its table) and `CUSTOMER_LOG.id`
    (an `id` on a table whose key is not the FK target) — both invite a join onto
    the wrong column."""
    probes = [ctx.probe(q) for q in (
        "list Customer Customer", "CUSTOMER_LOG by action",
        "how many CUSTOMER_LOG per customer_id")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_undeclared_joins(ctx: SuiteContext) -> None:
    """No FKs declared. `line-item.key -> order.key` needs a hyphenated table and
    a reserved-word join column."""
    probes = [ctx.probe(q) for q in (
        "line-item by group", "total qty by group", "order by group")]
    ctx.check(inv.check_executes(probes))
    _no_write(ctx, probes)


def suite_stress(ctx: SuiteContext) -> None:
    """Nothing here should crash or produce a write; a clean refusal is a pass."""
    probes = [ctx.probe(q) for q in (
        "select from where group by order",
        "\"order\"; DROP TABLE \"Customer\"; --",
        "how many 顧客 顧客 顧客",
        "total " + "value " * 50,
        "",
        "   ",
        "précio précio précio",
    )]
    _no_write(ctx, probes)


SUITES: dict[str, Callable[[SuiteContext], None]] = {
    "reserved_words": suite_reserved_words,
    "quoted_and_spaced": suite_quoted_and_spaced,
    "case_sensitivity": suite_case_sensitivity,
    "near_identical_columns": suite_near_identical_columns,
    "non_ascii": suite_non_ascii,
    "long_identifier": suite_long_identifier,
    "shadowed_names": suite_shadowed_names,
    "undeclared_joins": suite_undeclared_joins,
    "stress": suite_stress,
}
