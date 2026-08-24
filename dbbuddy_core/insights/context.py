"""Phase 2 — the bounded context builder.

Never send a database. This turns an executed result set into a compact evidence
packet: column shapes, per-numeric statistics, and a small row sample. Bounded on
every axis, so context size is governed by the *schema* of the result, not by how
many rows the query happened to return.

The same shaped context is what the cache hashes, so "same query, same data" is a
hit while "same query, changed data" is a miss.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import ColumnProfile, ResultContext, settings


def _is_numeric(value) -> bool:
    # bool is an int subclass, but a flag column is not a measure — treating it as
    # numeric would produce meaningless "mean 0.4" statistics.
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _as_float(value) -> float | None:
    """Coerce to a float, rejecting anything not finite.

    NaN and ±Infinity are not JSON: ``json.dumps`` emits the bare tokens ``NaN`` /
    ``Infinity``, which ``JSON.parse`` rejects in the browser and which PostgreSQL
    refuses to store in a ``json``/``jsonb`` column. A single NaN in one cell would
    otherwise poison the prompt, the API response, and the cache write. SQLite
    accepts them silently, so the test suite alone would never have shown this.
    """
    if _is_numeric(value):
        try:
            number = float(value)
        except (ValueError, OverflowError, InvalidOperation):
            return None
        return number if math.isfinite(number) else None
    return None


def _infer_kind(values: list) -> str:
    """Classify a column from its non-null values. Ordering matters: dates are
    checked before numerics because a date is not a measure to average."""
    present = [v for v in values if v is not None]
    if not present:
        return "text"
    if any(isinstance(v, (datetime, date)) for v in present):
        return "date"
    if all(_is_numeric(v) for v in present):
        return "numeric"
    return "text"


def _jsonable(value, max_chars: int):
    """Coerce one cell into something JSON-serializable and bounded in size."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, (float, Decimal)):
        # Same non-finite guard as the statistics path — a NaN cell must not reach
        # the prompt, the response, or the cache write as an unparseable token.
        number = _as_float(value)
        return number if number is not None else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    text = str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def _distinct_count(values: list) -> int:
    """Count distinct values, tolerating unhashable cells (dicts from JSON columns)
    by falling back to their repr rather than raising mid-build."""
    seen = set()
    for v in values:
        if v is None:
            continue
        try:
            seen.add(v)
        except TypeError:
            seen.add(repr(v))
    return len(seen)


def _profile_column(name: str, values: list) -> ColumnProfile:
    kind = _infer_kind(values)
    profile = ColumnProfile(
        name=name,
        kind=kind,
        null_count=sum(1 for v in values if v is None),
        distinct_count=_distinct_count(values),
    )
    if kind != "numeric":
        return profile

    numbers = [n for n in (_as_float(v) for v in values) if n is not None]
    if not numbers:
        return profile

    profile.minimum = min(numbers)
    profile.maximum = max(numbers)
    profile.total = sum(numbers)
    profile.mean = profile.total / len(numbers)
    profile.first, profile.last = numbers[0], numbers[-1]
    profile.change = profile.last - profile.first
    # Percent change is undefined against a zero baseline — leave it unset rather
    # than emitting an infinity the model would happily narrate as a real trend.
    if profile.first:
        profile.change_pct = (profile.change / abs(profile.first)) * 100.0
    return profile


def build_context(
    *,
    question: str,
    sql: str,
    database: str,
    rows: list[dict],
    chart_type: str | None = None,
    config=None,
) -> ResultContext:
    """Shape an executed result into the evidence packet sent to the model.

    ``rows`` is the result as the API returns it (a list of column→value dicts).
    Statistics are computed over **every** row supplied, while only the first
    ``max_sample_rows`` are included verbatim — so the model gets accurate totals
    for a large result without being handed the whole thing.
    """
    cfg = config or settings
    rows = rows or []
    row_count = len(rows)

    # Union the keys in first-seen order: a sparse result (rows with differing
    # keys) still profiles every column, and column order stays stable/meaningful.
    column_names: list[str] = []
    for row in rows:
        for key in row:
            if key not in column_names:
                column_names.append(key)
    truncated = len(column_names) > cfg.max_columns
    column_names = column_names[: cfg.max_columns]

    columns = [_profile_column(name, [r.get(name) for r in rows]) for name in column_names]

    sample_rows = [
        {name: _jsonable(row.get(name), cfg.max_cell_chars) for name in column_names}
        for row in rows[: cfg.max_sample_rows]
    ]

    return ResultContext(
        question=question,
        sql=sql,
        database=database,
        row_count=row_count,
        columns=columns,
        sample_rows=sample_rows,
        chart_type=chart_type,
        truncated=truncated or row_count > cfg.max_sample_rows,
    )


def _number_key(value) -> str | None:
    """Canonical integer-part key for a number, for value-grounding comparison.

    A model may render the same figure many ways ("108928421890", "108,928,421,890",
    "108.93B", "108.9"). We cannot chase every format, so we compare on the integer
    part with sign and separators stripped — enough to tell a real result value
    from a fabricated one, which is the whole job.
    """
    number = _as_float(value)
    if number is None:
        return None
    return str(int(abs(number)))


def grounded_numbers(context: ResultContext) -> set[str]:
    """Every number the result actually contains, as integer-part keys.

    Drawn from the per-column statistics, the sampled cells, and the row count —
    i.e. exactly the figures the model was shown. A causal clause that cites a
    number found here is grounded in the data; one that cites a number found
    nowhere (``"because of 47 supply-chain disruptions"``) is not, and the
    validator can then reject it instead of trusting any digit as evidence.
    """
    keys: set[str] = set()
    key = _number_key(context.row_count)
    if key is not None:
        keys.add(key)

    for col in context.columns:
        for stat in (col.minimum, col.maximum, col.mean, col.total,
                     col.first, col.last, col.change, col.change_pct):
            key = _number_key(stat)
            if key is not None:
                keys.add(key)

    for row in context.sample_rows:
        for value in row.values():
            key = _number_key(value)
            if key is not None:
                keys.add(key)
    return keys


def result_hash(context: ResultContext) -> str:
    """A stable hash of the shaped context — the cache's data-identity component.

    Hashing the *shaped* context rather than the raw rows means the hash covers
    exactly what the model was shown: statistics included, unbounded row data
    excluded. ``question`` is excluded because it is keyed separately alongside
    the SQL, and identical SQL over identical data should hit regardless of how
    the question was phrased.
    """
    payload = context.to_dict()
    payload.pop("question", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
