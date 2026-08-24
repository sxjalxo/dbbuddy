"""Correctness checks that need no golden answer.

The oracle problem is what kills NL-to-SQL dogfooding: you can generate a thousand
queries, but deciding whether each answer is *right* means someone reading a
thousand result sets. So this module scores what can be scored without one.

Three tiers, cheapest first:

``EXECUTES``
    The SQL ran. Catches compiler and dialect bugs — free, and the only tier that
    needs a single query.

``INVARIANT``
    A relationship between *two or more* answers that must hold whatever the data
    says. No expected value is ever written down, so a new database costs nothing
    to add. This is the tier that catches wrong-but-valid SQL, and in particular
    join fan-out double-counting: the single most common silent defect in
    generated SQL, where joining a 1:N table before aggregating multiplies the
    measure by the number of child rows.

``GOLDEN``
    A hand-checked expected result. Expensive per case, so reserved for a small
    curated set — and only worth writing once the tiers above are quiet.

An invariant that fails is not automatically an engine bug: the pair of questions
may simply have been planned as different questions. That is why every failure
carries both SQL strings — the diff is usually the diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Sequence

# Numbers that come back from different aggregation paths (SUM over a grouped
# query vs a single total) differ in the last bits for float columns, and in
# scale for DECIMAL. Compare relatively.
REL_TOLERANCE = 1e-6


@dataclass
class Probe:
    """One natural-language question and the SQL/rows it produced."""

    question: str
    sql: str | None = None
    rows: list[dict] | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.rows is not None

    def scalar(self) -> Any:
        """The single value a 'how many / total' question should return.

        Returns None when the shape is not a single value — which is itself a
        finding, and one the caller reports rather than papering over.
        """
        if not self.rows or len(self.rows) != 1:
            return None
        values = list(self.rows[0].values())
        return values[0] if len(values) == 1 else None


@dataclass
class Finding:
    """One failed check. ``kind`` is the tier; ``detail`` is the diagnosis."""

    kind: str
    name: str
    detail: str
    probes: list[Probe] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"[{self.kind}] {self.name}: {self.detail}"]
        for p in self.probes:
            lines.append(f"    Q: {p.question}")
            lines.append(f"    SQL: {p.sql or '<none>'}")
            if p.error:
                lines.append(f"    ERR: {p.error}")
        return "\n".join(lines)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def close(a: Any, b: Any, tolerance: float = REL_TOLERANCE) -> bool:
    """Relative comparison that treats 0 and near-0 sensibly."""
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return a == b
    scale = max(abs(x), abs(y), 1.0)
    return abs(x - y) <= tolerance * scale


# ── Tier 1: it ran ───────────────────────────────────────────────────────────

def check_executes(probes: Sequence[Probe]) -> list[Finding]:
    """Every probe produced SQL and that SQL ran."""
    out = []
    for p in probes:
        if p.error:
            out.append(Finding("EXECUTES", "query failed", p.error, [p]))
        elif not p.sql:
            out.append(Finding("EXECUTES", "no SQL produced",
                               "the planner returned no statement", [p]))
    return out


# ── Tier 2: invariants ───────────────────────────────────────────────────────

# Rows the engine returns before truncating. A breakdown that hit this cap is a
# partial list and cannot be expected to sum to anything.
ROW_CAP = 1000


def check_breakdown_sums_to_total(total: Probe, breakdown: Probe,
                                  measure_index: int = -1,
                                  row_cap: int = ROW_CAP) -> list[Finding]:
    """``sum(grouped rows) == ungrouped total``.

    The highest-value check in the suite. A plan that joins a 1:N table before
    aggregating multiplies the measure by the child-row count, and the result
    looks entirely plausible — a revenue number that is 3.7x too high reads as a
    good quarter, not as a bug. Grouping usually changes the join shape, so the
    two paths disagree exactly when fan-out is present.
    """
    if not (total.ok and breakdown.ok):
        return []
    if row_cap and len(breakdown.rows) >= row_cap:
        # A grain finer than the row cap ("total quantity by order" over 5,000
        # orders) comes back truncated, and a truncated list sums to less than
        # the total for a reason that is not a bug. Same guard as
        # check_count_matches_row_count.
        return []
    total_value = _num(total.scalar())
    if total_value is None:
        return [Finding("INVARIANT", "breakdown sums to total",
                        "the total query did not return a single scalar",
                        [total, breakdown])]

    summed = 0.0
    for row in breakdown.rows:
        values = [v for v in row.values() if _num(v) is not None]
        if not values:
            return [Finding("INVARIANT", "breakdown sums to total",
                            "the breakdown has no numeric measure column",
                            [total, breakdown])]
        summed += _num(values[measure_index])

    if not close(total_value, summed):
        ratio = summed / total_value if total_value else float("inf")
        return [Finding(
            "INVARIANT", "breakdown sums to total",
            f"total={total_value:g} but the grouped rows sum to {summed:g} "
            f"({ratio:.3g}x) — a near-integer ratio points at join fan-out",
            [total, breakdown])]
    return []


def check_filter_narrows(unfiltered: Probe, filtered: Probe) -> list[Finding]:
    """Adding a filter never returns *more* rows than not having it."""
    if not (unfiltered.ok and filtered.ok):
        return []
    a, b = _num(unfiltered.scalar()), _num(filtered.scalar())
    if a is None or b is None:
        a, b = len(unfiltered.rows), len(filtered.rows)
    if b > a and not close(a, b):
        return [Finding("INVARIANT", "filter narrows",
                        f"unfiltered={a:g} but filtered={b:g} — a filter added rows",
                        [unfiltered, filtered])]
    return []


def check_paraphrases_agree(probes: Sequence[Probe]) -> list[Finding]:
    """Two phrasings of one question return the same answer.

    Catches brittleness in intent extraction: the engine is deterministic, so a
    wording change that moves the number means the *question* was parsed
    differently, not that the data moved.
    """
    answered = [p for p in probes if p.ok]
    if len(answered) < 2:
        return []
    first = answered[0]
    baseline = first.scalar()
    out = []
    for other in answered[1:]:
        value = other.scalar()
        if baseline is None and value is None:
            # Fall back to comparing row counts when neither is scalar.
            if len(first.rows) != len(other.rows):
                out.append(Finding(
                    "INVARIANT", "paraphrases agree",
                    f"{len(first.rows)} rows vs {len(other.rows)} rows",
                    [first, other]))
            continue
        if not close(baseline, value):
            out.append(Finding(
                "INVARIANT", "paraphrases agree",
                f"{baseline!r} vs {value!r} for the same question",
                [first, other]))
    return out


def check_redundant_filter_is_noop(base: Probe, with_redundant: Probe) -> list[Finding]:
    """A filter that excludes nothing must not change the answer.

    e.g. ``WHERE amount > -1`` on a non-negative column. Catches a filter being
    compiled into the wrong clause, or forcing a join that changes cardinality.
    """
    if not (base.ok and with_redundant.ok):
        return []
    a, b = base.scalar(), with_redundant.scalar()
    if a is not None and b is not None and not close(a, b):
        return [Finding("INVARIANT", "redundant filter is a no-op",
                        f"{a!r} became {b!r} after adding a filter that excludes nothing",
                        [base, with_redundant])]
    if a is None and b is None and len(base.rows) != len(with_redundant.rows):
        return [Finding("INVARIANT", "redundant filter is a no-op",
                        f"{len(base.rows)} rows became {len(with_redundant.rows)}",
                        [base, with_redundant])]
    return []


def check_count_matches_row_count(count_probe: Probe, list_probe: Probe,
                                  limit: int | None = None) -> list[Finding]:
    """``count of X`` agrees with how many rows ``list X`` returns.

    Skipped when the listing hit its row cap, since a truncated list legitimately
    disagrees.
    """
    if not (count_probe.ok and list_probe.ok):
        return []
    counted = _num(count_probe.scalar())
    if counted is None:
        return []
    listed = len(list_probe.rows)
    if limit is not None and listed >= limit:
        return []
    if not close(counted, listed):
        return [Finding("INVARIANT", "count matches listing",
                        f"count says {counted:g}, listing returned {listed}",
                        [count_probe, list_probe])]
    return []


# ── Tier 3: golden ───────────────────────────────────────────────────────────

def check_golden(probe: Probe, expected: Any) -> list[Finding]:
    """A hand-verified expected scalar."""
    if not probe.ok:
        return []
    got = probe.scalar()
    if not close(got, expected):
        return [Finding("GOLDEN", "expected value",
                        f"expected {expected!r}, got {got!r}", [probe])]
    return []


# ── Tier 4: calibration ──────────────────────────────────────────────────────

def check_not_confident(probes: Sequence[Probe]) -> list[Finding]:
    """A question with more than one defensible reading must not read as certain.

    These probes name a column that exists on several tables (``unit_price`` on
    both ``products`` and ``order_items``). Picking one is fine — every answer
    here is defensible — but reporting it as *high* confidence is not: the number
    then flows unqualified into charts, dashboards and AI insights, which is the
    failure mode this tier exists to catch. Anything at or below "medium", or an
    explicit ambiguity note, counts as calibrated.
    """
    out = []
    for p in probes:
        if not p.ok:
            continue
        confidence = str(p.meta.get("confidence") or "").lower()
        if confidence == "high" and not p.meta.get("ambiguities"):
            out.append(Finding("CONFIDENCE", "certain about an ambiguous question",
                               f"reported confidence={confidence!r} with no "
                               f"ambiguity recorded", [p]))
    return out


# ── Scoring ──────────────────────────────────────────────────────────────────

@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    probes_run: int = 0
    checks_run: int = 0

    def add(self, findings: list[Finding]) -> None:
        self.checks_run += 1
        self.findings.extend(findings)

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out

    def summary(self) -> str:
        if not self.findings:
            return (f"PASS — {self.probes_run} probes, {self.checks_run} checks, "
                    "no findings")
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self.by_kind().items()))
        return (f"FAIL — {self.probes_run} probes, {self.checks_run} checks, "
                f"{len(self.findings)} findings ({counts})")


def run_checks(pairs: Sequence[tuple[Callable, tuple]]) -> Report:
    """Run ``(check_fn, args)`` pairs and collect every finding."""
    report = Report()
    for fn, args in pairs:
        report.add(fn(*args))
    return report
