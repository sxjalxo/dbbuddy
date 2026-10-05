"""Clause extraction that cannot be made quadratic by its input.

Several places pulled a clause out of SQL or a question with `KEYWORD\\s+(.+?)\\s+END`.
That shape is quadratic when the terminator is absent: the engine restarts at every
position where the opening keyword matches, and each restart scans to the end.
Repeating the keyword is enough — `"select " * 3000` took **481 ms** in one call:

    select..from   n=1500  125.78ms   n=3000  481.03ms   3.8x

Uncontrolled input reaches all of them. `/execute` takes raw SQL from a permitted
caller, and `ranking_dimension_phrase` reads the natural-language question
directly, so a request can cost the server seconds of CPU with no database
involved. CodeQL flagged the family; measuring it is what said which members were
real.

The fix is not a cleverer regex — the cost is the number of *start positions*, not
ambiguity inside the pattern. Two anchored scans replace it: find the opening
keyword once, then find the terminator once, forward from there. Linear, and the
same span.
"""

import re
import time

import pytest

from dbbuddy_core.text_spans import clause_between

SELECT = re.compile(r"\bselect\b", re.IGNORECASE)
FROM = re.compile(r"\bfrom\b", re.IGNORECASE)
WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)
LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)


# ── The span itself ───────────────────────────────────────────────────────────

def test_extracts_what_lies_between():
    assert clause_between("SELECT a, b FROM t", SELECT, [FROM]) == "a, b"


def test_the_span_is_stripped():
    assert clause_between("SELECT   a, b   FROM t", SELECT, [FROM]) == "a, b"


def test_newlines_count_as_separators():
    # The regex form used DOTALL for exactly this; formatted SQL is normal.
    assert clause_between("SELECT a,\n  b\nFROM t", SELECT, [FROM]) == "a,\n  b"


def test_the_first_terminator_wins():
    assert clause_between("SELECT a FROM t FROM u", SELECT, [FROM]) == "a"


def test_several_terminators_take_the_earliest():
    sql = "WHERE a = 1 LIMIT 10"
    assert clause_between(sql, WHERE, [LIMIT, FROM], required_end=False) == "a = 1"


def test_a_missing_opening_keyword_gives_none():
    assert clause_between("no keywords here", SELECT, [FROM]) is None


def test_a_missing_terminator_gives_none_when_required():
    # `select\\s+(.+?)\\s+from` matched nothing without a FROM, and callers relied
    # on that: a SELECT list is not meaningful if the statement has no FROM.
    assert clause_between("SELECT a, b", SELECT, [FROM]) is None


def test_a_missing_terminator_runs_to_the_end_when_optional():
    # `WHERE\\s+(.+?)(?:\\s+LIMIT|$)` — the trailing `$` alternative.
    assert clause_between("WHERE a = 1", WHERE, [LIMIT], required_end=False) == "a = 1"


def test_an_empty_span_is_returned_as_empty_not_none():
    # Distinguishable from "no match": the clause was there and was blank.
    assert clause_between("SELECT FROM t", SELECT, [FROM]) == ""


def test_a_word_boundary_is_respected():
    # "fromage" is not a FROM. The old patterns got this from `\\s+from`; the
    # replacement has to get it from `\\b`.
    assert clause_between("SELECT a fromage FROM t", SELECT, [FROM]) == "a fromage"


def test_searching_starts_at_the_offset_when_given():
    sql = "SELECT a FROM t SELECT b FROM u"
    assert clause_between(sql, SELECT, [FROM], start=10) == "b"


# ── The property that made this worth changing ────────────────────────────────

# Built inside the test rather than inlined: pytest puts the parameter into the
# PYTEST_CURRENT_TEST environment variable, and a 140 KB id exceeds the Windows
# limit on one — the test errors before it runs anything.
@pytest.mark.parametrize("keyword", ["select ", "SET ", "where "])
def test_a_pathological_input_stays_fast(keyword):
    payload = keyword * 20_000
    # A generous ceiling rather than a ratio: this is a regression guard, not a
    # benchmark, and it should not fail because a laptop was busy. The regex form
    # needed ~0.5 s at 3,000 repetitions; this is 20,000.
    started = time.perf_counter()
    clause_between(payload, SELECT, [FROM])
    clause_between(payload, WHERE, [LIMIT], required_end=False)
    assert time.perf_counter() - started < 1.0


def test_a_long_ordinary_statement_is_not_slow():
    sql = "SELECT " + ", ".join(f"col_{i}" for i in range(20_000)) + " FROM t"
    started = time.perf_counter()
    span = clause_between(sql, SELECT, [FROM])
    assert time.perf_counter() - started < 1.0
    assert span.startswith("col_0") and span.endswith("col_19999")
