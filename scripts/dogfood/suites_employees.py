"""Probe suites for the real MySQL ``employees`` sample database (~4M rows).

Same invariant discipline as the synthetic datasets, aimed at what a real
operational schema at scale stresses:

* the 2.8M-row ``salaries`` fact under a 300k ``employees`` dimension — the
  breakdown-sums-to-total invariant here is worth millions if a join fans out,
* declared foreign keys named ``_no`` (never ``_id``), so only the declared-FK
  and declared-PK graph tiers can relate the tables,
* composite keys with no surrogate ``id`` to group on,
* a ``CHAR(4)`` string key and an ``ENUM``-style gender dimension,
* columns (``emp_no``, ``dept_no``, ``from_date``, ``to_date``) that live on
  four or five tables at once — the calibration tier.

Golden values are never hard-coded; ``ctx.truth`` computes each from hand-written
SQL against the same database, so the numbers cannot drift out of sync.
"""

from __future__ import annotations

from typing import Callable

from scripts.dogfood import invariants as inv
from scripts.dogfood.suites import SuiteContext


def suite_basics(ctx: SuiteContext) -> None:
    probes = [ctx.probe(q) for q in (
        "how many employees", "how many departments", "how many salaries",
        "how many titles", "list departments")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_golden(ctx.probe("how many employees"),
                               ctx.truth("SELECT COUNT(*) FROM employees")))
    ctx.check(inv.check_golden(ctx.probe("how many departments"),
                               ctx.truth("SELECT COUNT(*) FROM departments")))
    ctx.check(inv.check_golden(ctx.probe("how many salaries"),
                               ctx.truth("SELECT COUNT(*) FROM salaries")))


def suite_gender_filter(ctx: SuiteContext) -> None:
    """``gender`` is a two-value ('M'/'F') dimension whose values are single-letter
    codes. A query naming the stored code must ground onto the column and narrow
    the count — the value is sampled into the index, so no domain knowledge is
    assumed. (Grounding the English synonym "female" -> 'F' would need a synonym
    layer this engine deliberately does not hardcode; that is out of scope here.)"""
    total = ctx.probe("how many employees")
    women = ctx.probe("how many employees with gender F")
    ctx.check(inv.check_executes([total, women]))
    ctx.check(inv.check_filter_narrows(total, women))
    ctx.check(inv.check_golden(
        women, ctx.truth("SELECT COUNT(*) FROM employees WHERE gender='F'")))


def suite_salary_aggregates(ctx: SuiteContext) -> None:
    """MAX/MIN/AVG over the 2.8M-row fact table, each pinned to ground truth."""
    checks = [
        ("highest salary", "SELECT MAX(salary) FROM salaries"),
        ("lowest salary",  "SELECT MIN(salary) FROM salaries"),
        ("average salary", "SELECT AVG(salary) FROM salaries"),
        ("total salary",   "SELECT SUM(salary) FROM salaries"),
    ]
    for question, truth_sql in checks:
        p = ctx.probe(question)
        ctx.check(inv.check_executes([p]))
        ctx.check(inv.check_golden(p, ctx.truth(truth_sql)))


def suite_fanout(ctx: SuiteContext) -> None:
    """The headline invariant: a salary breakdown by gender must sum back to the
    ungrouped total. salaries -> employees -> gender is a clean 1:N:1 path (each
    salary row belongs to exactly one employee, hence one gender), so the sum is
    conserved; a plan that fans out on the join would inflate it and no validator
    would notice. (Breaking it down by *department* is deliberately avoided — an
    employee's dept_emp history is M:N, so salary is not owned by a department and
    that question has no total-preserving answer.)"""
    total = ctx.probe("total salary")
    by_gender = ctx.probe("total salary by gender")
    ctx.check(inv.check_executes([total, by_gender]))
    ctx.check(inv.check_breakdown_sums_to_total(total, by_gender))
    # Guard against a *false* green: if "by gender" silently failed to group, the
    # breakdown would be a single grand-total row that trivially sums to itself.
    # gender has two values, so a real grouping returns two rows.
    from scripts.dogfood.invariants import Finding
    if by_gender.ok and len(by_gender.rows) < 2:
        ctx.check([Finding("INVARIANT", "breakdown did not group",
                           f"'by gender' returned {len(by_gender.rows)} row(s); "
                           f"expected one per gender — grouping was dropped",
                           [by_gender])])


def suite_paraphrase(ctx: SuiteContext) -> None:
    probes = [ctx.probe(q) for q in (
        "how many employees",
        "count employees",
        "number of employees",
        "total employees")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_paraphrases_agree(probes))


def suite_topn(ctx: SuiteContext) -> None:
    probes = [ctx.probe(q) for q in (
        "top 5 departments by headcount",
        "employees by department",
        "titles by count",
        "employees hired per year")]
    ctx.check(inv.check_executes(probes))


def suite_grouped_golden(ctx: SuiteContext) -> None:
    """A grouped breakdown must land on the right grain *and* the right numbers —
    the case a dropped GROUP BY or a leaked hidden key silently corrupts. Values
    come from ground-truth SQL, so this catches both 'did not group' (too few
    rows) and 'grouped too finely' (per-employee instead of per-title)."""
    from scripts.dogfood.invariants import Finding

    # Salary by gender: two rows, and the female total matches ground truth.
    by_gender = ctx.probe("total salary by gender")
    ctx.check(inv.check_executes([by_gender]))
    n_gender = ctx.truth("SELECT COUNT(DISTINCT gender) FROM employees")
    if by_gender.ok and len(by_gender.rows) != n_gender:
        ctx.check([Finding("GOLDEN", "wrong grouping grain",
                           f"salary by gender returned {len(by_gender.rows)} rows, "
                           f"expected {n_gender}", [by_gender])])

    # Headcount per title: rows == number of distinct titles (7), not per-employee.
    by_title = ctx.probe("top 100 titles by count")
    ctx.check(inv.check_executes([by_title]))
    n_titles = ctx.truth("SELECT COUNT(DISTINCT title) FROM titles")
    if by_title.ok and len(by_title.rows) != n_titles:
        ctx.check([Finding("GOLDEN", "wrong grouping grain",
                           f"titles by count returned {len(by_title.rows)} rows, "
                           f"expected {n_titles} distinct titles", [by_title])])


def suite_having(ctx: SuiteContext) -> None:
    """HAVING over a grouped join on a keyless-``id`` schema — the exact shape
    that crashed when the COUNT target defaulted to a surrogate ``employees.id``
    that does not exist. Must run and land the right department count."""
    p = ctx.probe("departments with more than 20000 employees")
    ctx.check(inv.check_executes([p]))
    # The probe returns one row per qualifying department; ground truth counts how
    # many there should be. A crash (the old ``employees.id`` bug), a dropped
    # HAVING, or a wrong join grain all land a different row count here.
    from scripts.dogfood.invariants import Finding
    n = ctx.truth("SELECT COUNT(*) FROM (SELECT d.dept_no FROM dept_emp de "
                  "JOIN departments d ON de.dept_no=d.dept_no "
                  "GROUP BY d.dept_no HAVING COUNT(*) > 20000)")
    if p.ok and len(p.rows) != n:
        ctx.check([Finding("GOLDEN", "wrong HAVING row count",
                           f"got {len(p.rows)} departments, expected {n}", [p])])


def suite_determinism(ctx: SuiteContext) -> None:
    """The same question must compile to the same SQL. ``unit_price``-style column
    names that live on two tables used to resolve through ``list(set(...))``, whose
    hash-order iteration differs per process — so an identical query returned a
    different number on a re-run, which is unacceptable in an ERP. Ask twice and
    require byte-identical SQL."""
    from scripts.dogfood.invariants import Finding
    a = ctx.probe("average salary by title")
    b = ctx.probe("average salary by title")
    ctx.check(inv.check_executes([a, b]))
    if a.sql and b.sql and a.sql != b.sql:
        ctx.check([Finding("INVARIANT", "non-deterministic SQL",
                           f"same question compiled two ways:\n  {a.sql}\n  {b.sql}",
                           [a, b])])


def suite_composite_keys(ctx: SuiteContext) -> None:
    """No table here has a surrogate ``id``; grouping and counting must resolve
    onto a real key or key component, not a column that does not exist."""
    probes = [ctx.probe(q) for q in (
        "how many current titles",
        "count dept_emp",
        "list titles")]
    ctx.check(inv.check_executes(probes))


def suite_confidence(ctx: SuiteContext) -> None:
    """``from_date`` lives on salaries, titles, dept_emp and dept_manager;
    ``dept_no`` on departments, dept_emp and dept_manager. A bare mention names
    no single table, so the answer must not read as certain."""
    probes = [ctx.probe(q) for q in (
        "earliest from date",
        "count dept_no",
        "latest to date")]
    ctx.check(inv.check_executes(probes))
    ctx.check(inv.check_not_confident(probes))


def suite_stress(ctx: SuiteContext) -> None:
    """A strange or hostile question must fail cleanly — never crash, never
    compile to a write."""
    from scripts.dogfood.invariants import Finding
    probes = [
        ctx.probe("employees'; DROP TABLE salaries; --"),
        ctx.probe("SELECT * FROM salaries"),
        ctx.probe("how many " + "senior " * 60 + "employees"),
        ctx.probe("平均工资"),
        ctx.probe("salary where salary > 100000 and salary < 1000"),
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
    "gender_filter": suite_gender_filter,
    "salary_aggregates": suite_salary_aggregates,
    "fanout": suite_fanout,
    "grouped_golden": suite_grouped_golden,
    "paraphrase": suite_paraphrase,
    "topn": suite_topn,
    "having": suite_having,
    "determinism": suite_determinism,
    "composite_keys": suite_composite_keys,
    "confidence": suite_confidence,
    "stress": suite_stress,
}
