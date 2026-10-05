"""Pull a clause out of text without letting the text decide how long it takes.

Several places extracted a clause with ``KEYWORD\\s+(.+?)\\s+END`` — the SELECT
list, a GROUP BY, a WHERE, the dimension in "top N X by Y". That shape is
**quadratic** when the terminator is missing: the engine restarts at every
position where the opening keyword matches, and each restart scans to the end of
the input. Repeating the keyword is all it takes:

    select..from   n=1500  125.78ms   n=3000  481.03ms   3.8x

The input is not ours. ``/execute`` accepts raw SQL from a permitted caller, and
``ranking_dimension_phrase`` reads the natural-language question directly, so one
request can spend seconds of CPU before a database is even contacted.

**A cleverer regex does not fix it.** The cost comes from the number of starting
positions, not from ambiguity inside the pattern — rewriting the boundaries as
``\\bselect\\b(.*?)\\bfrom\\b`` measured just as quadratic. What fixes it is doing
two anchored searches instead of one backtracking scan: find the opening keyword
once, then find the terminator once, searching forward from there.

This is not a SQL parser and does not pretend to be. It is the same textual
heuristic as before, with the pathological case removed.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence


def clause_between(text: str, opening: "re.Pattern[str]", ends: Sequence["re.Pattern[str]"],
                   *, required_end: bool = True, start: int = 0) -> Optional[str]:
    """The text between ``opening`` and the earliest of ``ends``, stripped.

    Returns None when ``opening`` does not match, and — with ``required_end`` —
    when no terminator follows it. That second case matters:
    ``select\s+(.+?)\s+from`` matched nothing at all without a FROM, and callers
    depend on the distinction, because a SELECT list means nothing in a statement
    that has no FROM.

    With ``required_end=False`` a missing terminator means "to the end", which is
    what the ``(?:\s+LIMIT|$)`` alternatives expressed.

    An empty result is a real answer (the clause was present and blank) and is
    returned as ``""``, never conflated with None.

    ``start`` is an offset into ``text``, so a caller scanning repeatedly does not
    have to slice — slicing a large statement per clause would trade quadratic
    matching for quadratic copying.
    """
    found_opening = opening.search(text, start)
    if found_opening is None:
        return None

    lo = found_opening.end()
    hi: Optional[int] = None
    for end in ends:
        found = end.search(text, lo)
        if found is not None and (hi is None or found.start() < hi):
            hi = found.start()

    if hi is None:
        if required_end:
            return None
        hi = len(text)

    return text[lo:hi].strip()
