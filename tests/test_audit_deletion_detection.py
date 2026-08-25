"""Gaps in the audit sequence — the half a per-row signature cannot see.

A signature proves a row was not *edited*. It says nothing about how many rows
there should be, so deleting one outright left no trace at all. A
database-assigned monotonic sequence closes that: the rows carry 1, 2, 3, … and a
missing 2 is visible without the application coordinating anything.

Two things this deliberately does not claim, both tested below:

* **A gap is a question, not a verdict.** A rolled-back transaction consumes a
  sequence value and leaves a hole in an entirely honest log. The report says
  "possible", and anything that phrased it as proof would be wrong.
* **The sequence value is not signed.** It cannot be — the database assigns it
  after the signature is computed. Someone who can delete a row can also renumber
  the rest. What that costs them is rewriting every later row instead of running
  one DELETE, and it leaves the sequence's own counter ahead of the data, which
  the tail check reports.

The analysis is a pure function over sequence values so it can be tested exactly,
including on SQLite, where the column exists but nothing assigns it.
"""

import pathlib
import sys

_BACKEND = str(pathlib.Path(__file__).resolve().parents[1] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app_db.audit_integrity import sequence_gaps  # noqa: E402


# ── Nothing wrong ─────────────────────────────────────────────────────────────

def test_a_contiguous_run_has_no_gaps():
    report = sequence_gaps([1, 2, 3, 4, 5])
    assert report.missing == 0
    assert report.ranges == []


def test_a_run_that_does_not_start_at_one_is_still_contiguous():
    # Only the rows still present are known. A log truncated by retention starts
    # wherever it starts, and that is not a gap.
    report = sequence_gaps([100, 101, 102])
    assert report.missing == 0
    assert report.first == 100 and report.last == 102


def test_a_single_row_has_no_gaps():
    report = sequence_gaps([7])
    assert report.missing == 0
    assert report.first == report.last == 7


def test_no_rows_at_all_reports_nothing_rather_than_failing():
    report = sequence_gaps([])
    assert report.missing == 0
    assert report.first is None and report.last is None
    assert report.present == 0


def test_input_need_not_be_sorted():
    # Rows come back ordered by created_at, which under concurrency is not
    # exactly sequence order.
    assert sequence_gaps([3, 1, 2]).missing == 0


# ── Gaps ──────────────────────────────────────────────────────────────────────

def test_one_missing_value_is_reported():
    report = sequence_gaps([1, 2, 4, 5])
    assert report.missing == 1
    assert report.ranges == [(3, 3)]


def test_consecutive_missing_values_collapse_into_one_range():
    # A range rather than a list: deleting a day of activity would otherwise
    # print a hundred thousand integers.
    report = sequence_gaps([1, 10])
    assert report.missing == 8
    assert report.ranges == [(2, 9)]


def test_several_separate_gaps_are_reported_separately():
    report = sequence_gaps([1, 3, 5, 9])
    assert report.ranges == [(2, 2), (4, 4), (6, 8)]
    assert report.missing == 5


def test_expected_and_present_describe_the_window():
    report = sequence_gaps([1, 2, 4, 5])
    assert report.present == 4
    assert report.expected == 5           # 1..5 inclusive
    assert report.missing == report.expected - report.present


# ── Anomalies that are not gaps ───────────────────────────────────────────────

def test_a_repeated_value_is_counted_and_does_not_hide_a_gap():
    # Two rows sharing a sequence value cannot happen from a sequence. If it
    # shows up, counting it as two present rows would cancel out a real gap.
    report = sequence_gaps([1, 2, 2, 4])
    assert report.duplicates == 1
    assert report.ranges == [(3, 3)]
    assert report.present == 3            # distinct values, not row count


def test_none_values_are_ignored_and_counted():
    # Rows written before the column existed, and every row on an engine that
    # does not assign one. Treating them as a gap would report an upgrade as
    # tampering.
    report = sequence_gaps([None, 1, 2, None, 3])
    assert report.unsequenced == 2
    assert report.missing == 0
    assert report.present == 3


def test_only_unsequenced_rows_means_no_analysis_not_a_clean_bill():
    report = sequence_gaps([None, None])
    assert report.unsequenced == 2
    assert report.present == 0
    assert report.first is None
    assert report.missing == 0


# ── The tail check ────────────────────────────────────────────────────────────

def test_the_counter_running_ahead_of_the_data_is_reported():
    # Renumbering to close a gap leaves the sequence's own counter ahead of
    # max(seq). Like a gap, it is a question: a rolled-back transaction does the
    # same thing.
    report = sequence_gaps([1, 2, 3], last_value=10)
    assert report.tail_missing == 7


def test_no_tail_discrepancy_when_the_counter_matches():
    assert sequence_gaps([1, 2, 3], last_value=3).tail_missing == 0


def test_tail_check_is_skipped_when_the_counter_is_unknown():
    # SQLite has no sequence to interrogate.
    assert sequence_gaps([1, 2, 3]).tail_missing == 0


def test_a_counter_behind_the_data_is_not_reported_as_missing():
    # Nonsensical, but it must not underflow into a negative "missing" count.
    assert sequence_gaps([1, 2, 9], last_value=3).tail_missing == 0
