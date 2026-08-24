"""Schema-driven, type-aware comparison extraction.

Turns natural-language comparisons into typed WHERE filters using each column's
declared **type**, not per-domain keyword lists. A ``TypeHandler`` per type knows
which operators are valid and how to parse a value; the planner only infers the
operator + typed value, and the existing Predicate AST / SQL compiler
(``dbbuddy_core.sql``) render the SQL. Adding a new type (boolean, enum, uuid,
json, …) means registering a handler — the planner never changes.

Design contract:
* **Schema-driven** — columns and their types come from the connected database.
* **Deterministic** — pure parsing, no LLM.
* **Fail closed** — an ordering comparison against a type that can't support it
  (e.g. ``name > 5``) raises ``InvalidComparisonError`` rather than guessing.
* **Compiler-owned SQL** — handlers emit ``{column, operator, value}`` specs only.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple


class InvalidComparisonError(ValueError):
    """A comparison operator was applied to a column whose type can't support it."""


# ── SQL type classification ───────────────────────────────────────────────────
# Map a raw engine type string (``varchar(255)``, ``DECIMAL(10,2)``, ``timestamp
# without time zone``) to a coarse category that selects a handler.

_NUMERIC_PREFIXES = (
    "int", "integer", "bigint", "smallint", "tinyint", "mediumint", "dec",
    "decimal", "numeric", "number", "float", "double", "real", "money", "serial",
    "bit",
)
_DATE_PREFIXES = ("date", "datetime", "timestamp", "time", "year", "interval")
_BOOL_PREFIXES = ("bool", "boolean")


def classify_sql_type(raw: Optional[str]) -> str:
    """Return one of ``numeric`` | ``date`` | ``boolean`` | ``string``."""
    if not raw:
        return "string"
    base = re.split(r"[ (]", raw.strip().lower(), maxsplit=1)[0]
    if base.startswith(_BOOL_PREFIXES):
        return "boolean"
    if base.startswith(_DATE_PREFIXES):
        return "date"
    if base.startswith(_NUMERIC_PREFIXES):
        return "numeric"
    return "string"


# ── Words that are never a literal value (schema-independent part) ────────────
_STOPWORDS = frozenset({
    "give", "me", "show", "get", "find", "list", "display", "fetch", "tell",
    "return", "lookup", "look", "search", "all", "the", "a", "an", "of", "for",
    "to", "about", "please", "with", "and", "or", "from", "in", "is", "are", "by",
    "on", "at", "as", "details", "detail", "info", "information", "record",
    "records", "data", "where", "who", "whose", "that", "this", "these", "those",
    # Marker words that introduce a value but are never a value themselves —
    # otherwise "customer named X" captures "named" as the customer value.
    "named", "called", "name",
})
_TEMPORAL = frozenset({
    "last", "next", "previous", "past", "recent", "today", "yesterday", "month",
    "monthly", "week", "weekly", "year", "yearly", "day", "daily", "quarter",
    "quarterly", "ago", "hour", "minute", "since",
})


# Grammar tokens are the ONLY exclusion source. Deliberately *not* derived from
# schema identifiers: splitting column names (e.g. "is_paid" → "is","paid") made a
# valid value like "paid" suddenly excluded just because an unrelated column
# existed — planner behavior must not depend on schema evolution (determinism). A
# value that happens to equal a real column/table name is rejected separately by
# an exact-name check in the driver, never by fragment matching.
_GRAMMAR_TOKENS = frozenset(_STOPWORDS | _TEMPORAL)


def _is_string_value(token: str) -> bool:
    t = token.lower()
    return len(t) >= 2 and not t.isdigit() and t not in _GRAMMAR_TOKENS


_ISO_DATE = r"\d{4}(?:-\d{2}-\d{2})?"

# Suffixes NL routinely drops when naming a column: temporal ("created_at" →
# "created") and engineering units / currencies ("weight_kg" → "weight",
# "amount_usd" → "amount"). Derived from the name itself, not a domain vocabulary.
_DROPPABLE_SUFFIXES = (
    "_at", "_date", "_on", "_time", "_ts", "_datetime", "_dt",
    "_kg", "_g", "_mg", "_lb", "_oz", "_km", "_cm", "_mm", "_ms",
    "_pct", "_percent", "_usd", "_eur", "_gbp", "_jpy", "_inr", "_bps",
)


def _column_match_forms(column: str, other_columns=()) -> List[str]:
    """Natural phrasings of a column name: the name, its space form, and the name
    minus a droppable suffix. A stripped alias is skipped when it collides with a
    real column name elsewhere (so ``amount_usd`` isn't aliased to ``amount`` when
    an ``amount`` column also exists) — keeps matches unambiguous."""
    cl = column.lower()
    forms = {cl, cl.replace("_", " ")}
    others = {c.lower() for c in other_columns if c.lower() != cl}
    for suf in _DROPPABLE_SUFFIXES:
        if cl.endswith(suf) and len(cl) > len(suf):
            stem = cl[: -len(suf)]
            if stem not in others:
                forms.add(stem)
    return sorted(forms, key=len, reverse=True)


def _name_pattern(forms: List[str]) -> str:
    """Regex alternation matching any of a column's name forms."""
    return r"(?:" + "|".join(re.escape(f) for f in forms) + r")"


# ── Deterministic temporal expressions (Phase A: calendar, Phase B: relative) ─
# "today", "this month", "last quarter", "past 30 days" … resolve to a concrete
# half-open [start, end) date range computed at query time — bound as parameters,
# so no dialect date functions are needed and DATE/DATETIME/TIMESTAMP columns all
# behave correctly. Every expression here is deterministic; fuzzy phrases
# ("January", "Q1", "summer") are intentionally not handled (they'd need
# locale/business rules).

def _today() -> date:
    """Server's current date. Indirected so tests can pin it."""
    return date.today()


def _add_months(first_of_month: date, n: int) -> date:
    """Shift a first-of-month date by ``n`` months, staying on the 1st."""
    m0 = first_of_month.year * 12 + (first_of_month.month - 1) + n
    return date(m0 // 12, m0 % 12 + 1, 1)


def _quarter_start(d: date) -> date:
    """First day of the calendar quarter containing ``d``."""
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def _temporal_range(phrase: str, today: date) -> Optional[Tuple[date, date]]:
    """Half-open [start, end) range for a temporal phrase, or None.

    ``phrase`` is lower-cased and single-spaced. Rolling ``past/last/next N days``
    windows include today; named periods (this/last week|month|quarter|year) are
    whole calendar periods."""
    day = timedelta(days=1)
    first = today.replace(day=1)
    monday = today - timedelta(days=today.weekday())  # ISO week starts Monday

    named = {
        "today": (today, today + day),
        "yesterday": (today - day, today),
        "tomorrow": (today + day, today + 2 * day),
        "this week": (monday, monday + 7 * day),
        "last week": (monday - 7 * day, monday),
        "this month": (first, _add_months(first, 1)),
        "last month": (_add_months(first, -1), first),
        "this quarter": (_quarter_start(today), _add_months(_quarter_start(today), 3)),
        "last quarter": (_add_months(_quarter_start(today), -3), _quarter_start(today)),
        "this year": (date(today.year, 1, 1), date(today.year + 1, 1, 1)),
        "last year": (date(today.year - 1, 1, 1), date(today.year, 1, 1)),
    }
    if phrase in named:
        return named[phrase]

    # Rolling window: "past/last/next N days|weeks".
    m = re.fullmatch(r"(past|last|next)\s+(\d+)\s+(day|days|week|weeks)", phrase)
    if m:
        direction, n, unit = m.group(1), int(m.group(2)), m.group(3)
        if n <= 0:
            return None
        span = timedelta(days=n * (7 if unit.startswith("week") else 1))
        if direction == "next":
            return today, today + span          # from today, forward N days
        return today - span, today + day        # last N days, up to and incl. today
    return None


# Ordered so multi-word/named phrases match before the numeric rolling form.
_TEMPORAL_PHRASE = (
    r"today|yesterday|tomorrow"
    r"|this\s+week|this\s+month|this\s+quarter|this\s+year"
    r"|last\s+week|last\s+month|last\s+quarter|last\s+year"
    r"|(?:past|last|next)\s+\d+\s+(?:days?|weeks?)"
)


def _normalize_iso(value: str) -> Optional[str]:
    """Accept ``YYYY`` or ``YYYY-MM-DD``; expand a bare year to Jan 1st. Returns a
    validated ``YYYY-MM-DD`` string, or None if not a valid ISO date."""
    value = value.strip()
    if re.fullmatch(r"\d{4}", value):
        return f"{value}-01-01"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ── Type handlers ─────────────────────────────────────────────────────────────

class TypeHandler:
    """Extract comparison filters for one column category. Subclasses implement
    ``extract`` and return a list of ``{column, operator, value}`` specs."""

    category = "string"

    def extract(self, query: str, table: str, column: str, col_lower: str,
                name_re: str, excluded: frozenset, strict: bool) -> List[Dict]:
        raise NotImplementedError


class NumericTypeHandler(TypeHandler):
    category = "numeric"

    # word form → SQL operator
    _WORD_OPS = {
        "greater than": ">", "more than": ">", "over": ">", "above": ">",
        "less than": "<", "fewer than": "<", "under": "<", "below": "<",
        "at least": ">=", "no less than": ">=", "minimum": ">=", "min": ">=",
        "at most": "<=", "no more than": "<=", "maximum": "<=", "max": "<=",
        "up to": "<=", "equal to": "=", "equals": "=", "equal": "=",
    }
    # Optional leading currency symbol (not captured); thousands separators
    # allowed ("$1,000"). Parsing strips both — without this "$100" matched
    # nothing and "1,000" silently became 1.
    _NUM = r"(?:[$€£¥₹]\s?)?(-?\d[\d,]*(?:\.\d+)?)"

    @staticmethod
    def _num(text: str):
        text = text.replace(",", "")
        return int(text) if re.fullmatch(r"-?\d+", text) else float(text)

    def extract(self, query, table, column, col_lower, name_re, excluded, strict):
        col = name_re
        qualified = f"{table}.{column}"

        # BETWEEN a AND b
        m = re.search(rf"\b{col}\b\s+between\s+{self._NUM}\s+and\s+{self._NUM}",
                      query, re.IGNORECASE)
        if m:
            return [{"column": qualified, "operator": "BETWEEN",
                     "value": [self._num(m.group(1)), self._num(m.group(2))]}]

        # Symbol operators: col >= 30
        m = re.search(rf"\b{col}\b\s*(>=|<=|>|<|==|=)\s*{self._NUM}", query, re.IGNORECASE)
        if m:
            op = "=" if m.group(1) == "==" else m.group(1)
            return [{"column": qualified, "operator": op, "value": self._num(m.group(2))}]

        # Word operators: col over 100
        words = "|".join(sorted(self._WORD_OPS, key=len, reverse=True))
        m = re.search(rf"\b{col}\b\s+(?:is\s+)?({words})\s+{self._NUM}", query, re.IGNORECASE)
        if m:
            return [{"column": qualified, "operator": self._WORD_OPS[m.group(1).lower()],
                     "value": self._num(m.group(2))}]
        return []


class DateTypeHandler(TypeHandler):
    category = "date"

    _WORD_OPS = {
        "after": ">", "before": "<", "since": ">=", "from": ">=",
        "on or after": ">=", "until": "<=", "up to": "<=", "on or before": "<=",
        "on": "=",
    }

    def extract(self, query, table, column, col_lower, name_re, excluded, strict):
        col = name_re
        qualified = f"{table}.{column}"

        # Calendar expression: "created today", "orders this month" → a concrete
        # half-open [start, end) range, so it's correct for both DATE and DATETIME.
        m = re.search(
            rf"\b{col}\b\s+(?:(?:in|on|during|for)\s+)?({_TEMPORAL_PHRASE})",
            query, re.IGNORECASE,
        )
        if m:
            expr = re.sub(r"\s+", " ", m.group(1).strip().lower())
            rng = _temporal_range(expr, _today())
            if rng:
                start, end = rng
                return [
                    {"column": qualified, "operator": ">=", "value": start.isoformat()},
                    {"column": qualified, "operator": "<", "value": end.isoformat()},
                ]

        # BETWEEN d1 AND d2
        m = re.search(rf"\b{col}\b\s+between\s+({_ISO_DATE})\s+and\s+({_ISO_DATE})",
                      query, re.IGNORECASE)
        if m:
            lo, hi = _normalize_iso(m.group(1)), _normalize_iso(m.group(2))
            if lo and hi:
                return [{"column": qualified, "operator": "BETWEEN", "value": [lo, hi]}]
            return []

        # Symbol operators: col >= 2024-01-01
        m = re.search(rf"\b{col}\b\s*(>=|<=|>|<|=)\s*({_ISO_DATE})", query, re.IGNORECASE)
        if m:
            iso = _normalize_iso(m.group(2))
            if iso:
                return [{"column": qualified, "operator": m.group(1), "value": iso}]
            return []

        # Word operators: col after 2024-01-01 / since 2024
        words = "|".join(sorted(self._WORD_OPS, key=len, reverse=True))
        m = re.search(rf"\b{col}\b\s+({words})\s+({_ISO_DATE})", query, re.IGNORECASE)
        if m:
            iso = _normalize_iso(m.group(2))
            if iso:
                return [{"column": qualified, "operator": self._WORD_OPS[m.group(1).lower()],
                         "value": iso}]
        return []


# Ordering used with a symbol against a string column is unambiguous misuse.
_ORDERING_SYMBOL = re.compile(r"\s*(>=|<=|>|<)\s*\S")

# Words/punctuation that mean the token after them is a VALUE, not a modifier —
# so a boolean adjective like "paid" in "status = paid" / "in (paid, …)" /
# "status is not paid" must not be read as the is_paid flag.
_VALUE_INTRODUCERS = {"in", "is", "not", "or", "and", "="}


def _in_value_position(query: str, start: int) -> bool:
    pre = query[:start].rstrip()
    if not pre:
        return False
    if pre[-1] in "=:(,":
        return True
    last_word = re.search(r"[A-Za-z]+$", pre)
    return bool(last_word and last_word.group(0).lower() in _VALUE_INTRODUCERS)


class StringTypeHandler(TypeHandler):
    """Grammar-aware text predicates. Recognizes explicit constructs — ``is`` /
    ``is not`` / ``in (...)`` / ``contains`` / ``is null`` — *before* falling back
    to bare adjacency, so operator words like "not"/"contains"/"in" are never
    mistaken for values. Emits operators the compiler already renders."""

    category = "string"
    _VAL = r"[\"']?([A-Za-z0-9_][\w.@-]*)[\"']?"   # a single value token

    def extract(self, query, table, column, col_lower, name_re, excluded, strict):
        col = name_re
        q = qualified = None  # (silence linters)
        qualified = f"{table}.{column}"
        q = query

        def ok(v):  # a captured token is a real value, not a grammar word
            return _is_string_value(v)

        # Fail closed: an ordering *symbol* on a known string column (name > 5) is
        # invalid — never guess. Strict mode only (real types known).
        if strict:
            m = re.search(rf"\b{col}\b(.*)$", q, re.IGNORECASE)
            if m and _ORDERING_SYMBOL.match(m.group(1)):
                raise InvalidComparisonError(
                    f"Operator '{m.group(1).strip()[:2].strip()}' is not valid for "
                    f"text column '{qualified}'."
                )

        # IS [NOT] NULL — "no email", "without email", "email is null/empty",
        # "has email", "email is not null".
        if (re.search(rf"\b{col}\b\s+is\s+not\s+(?:null|empty|blank|set)\b", q, re.IGNORECASE)
                or re.search(rf"\bhas\s+(?:an?\s+)?{col}\b", q, re.IGNORECASE)):
            return [{"column": qualified, "operator": "IS NOT NULL", "value": None}]
        if (re.search(rf"\b(?:no|without|missing)\s+{col}\b", q, re.IGNORECASE)
                or re.search(rf"\b{col}\b\s+is\s+(?:null|empty|blank|missing|not\s+set)\b", q, re.IGNORECASE)):
            return [{"column": qualified, "operator": "IS NULL", "value": None}]

        # IN / NOT IN — parenthesized list only (so "orders in Germany" isn't a list).
        m = re.search(rf"\b{col}\b\s+(?:is\s+)?(not\s+)?in\s*\(([^)]+)\)", q, re.IGNORECASE)
        if m:
            items = [x.strip().strip("\"'") for x in re.split(r"[,;]| or ", m.group(2))]
            items = [x for x in items if ok(x)]
            if items:
                return [{"column": qualified, "operator": "NOT IN" if m.group(1) else "IN",
                         "value": items}]

        # LIKE — contains / starts with / ends with.
        m = re.search(rf"\b{col}\b\s+(?:contains|containing|like|matching)\s+{self._VAL}", q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "LIKE", "value": f"%{m.group(1)}%"}]
        m = re.search(rf"\b{col}\b\s+starts?\s+with\s+{self._VAL}", q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "LIKE", "value": f"{m.group(1)}%"}]
        m = re.search(rf"\b{col}\b\s+ends?\s+with\s+{self._VAL}", q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "LIKE", "value": f"%{m.group(1)}"}]

        # Negated equality — "is not X", "!=", "<>", "not equal to X".
        m = re.search(rf"\b{col}\b\s*(?:is\s+not|is\s+not\s+equal\s+to|not\s+equal\s+to|!=|<>)\s+{self._VAL}",
                      q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "!=", "value": m.group(1)}]

        # Equality — explicit operator: "= X", "is X", "equals X", ": X".
        m = re.search(rf"\b{col}\b\s*(?:=|==|:|\bis\b|\bequals?\b)\s+{self._VAL}", q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "=", "value": m.group(1)}]

        # Adjacency fallback — "<col> value" with no operator (e.g. "city Berlin").
        m = re.search(rf"\b{col}\b\s+{self._VAL}", q, re.IGNORECASE)
        if m and ok(m.group(1)):
            return [{"column": qualified, "operator": "=", "value": m.group(1)}]
        return []


class BooleanTypeHandler(TypeHandler):
    """Boolean predicates: explicit ``= true/false`` and natural adjective
    phrasing. The positive adjective is the column name minus an ``is_``/``has_``
    prefix — "paid orders" → ``is_paid = true``, "active users" → ``active = true``;
    "not paid"/"unpaid" → ``= false``. Derived from the column name, not a list."""

    category = "boolean"
    _TRUE = {"true", "yes", "1", "t", "y"}

    def extract(self, query, table, column, col_lower, name_re, excluded, strict):
        qualified = f"{table}.{column}"

        # Explicit: "<col> = true", "<col> is not false", …
        m = re.search(rf"\b{name_re}\b\s*(?:=|==|:|\bis\b)\s*(not\s+)?(true|false|yes|no|1|0|t|f|y|n)\b",
                      query, re.IGNORECASE)
        if m:
            val = m.group(2).lower() in self._TRUE
            if m.group(1):
                val = not val
            return [{"column": qualified, "operator": "=", "value": val}]

        # Adjective phrasing: positive adjective = column minus is_/has_ prefix,
        # matched in both underscore and space forms ("in_stock" ~ "in stock").
        # A match in a value position (after "=", "in (", "is not", …) is skipped —
        # there the word is a value for another column, not this flag.
        adj = re.sub(r"^(?:is|has)_?", "", col_lower).strip("_")
        if len(adj) >= 3:
            forms = {adj, adj.replace("_", " ")}
            adj_re = "(?:" + "|".join(re.escape(a) for a in sorted(forms, key=len, reverse=True)) + ")"
            for m in re.finditer(rf"\b(not|non[- ]?|un)?\s*{adj_re}\b", query, re.IGNORECASE):
                if _in_value_position(query, m.start()):
                    continue
                negated = bool(m.group(1))
                return [{"column": qualified, "operator": "=", "value": not negated}]
        return []


# ── Registry ──────────────────────────────────────────────────────────────────

_TYPE_HANDLERS: Dict[str, TypeHandler] = {
    "numeric": NumericTypeHandler(),
    "date": DateTypeHandler(),
    "boolean": BooleanTypeHandler(),
    "string": StringTypeHandler(),
}


def register_type_handler(handler: TypeHandler) -> None:
    """Register (or override) the handler for ``handler.category``. This is the
    single extension point for new column types — no planner change required."""
    _TYPE_HANDLERS[handler.category] = handler


def get_type_handler(category: str) -> TypeHandler:
    return _TYPE_HANDLERS.get(category, _TYPE_HANDLERS["string"])


def _pick_table(candidates: List[tuple], query_lower: str, query_words: set) -> tuple:
    """When a column name lives in several tables, prefer the one the query names."""
    if len(candidates) == 1:
        return candidates[0]
    for table, col in candidates:
        tl = table.lower()
        if tl in query_lower or tl.rstrip("s") in query_words:
            return (table, col)
    return candidates[0]


def extract_comparisons(
    query: str,
    schema: Dict[str, List[str]],
    column_types: Optional[Dict[str, Dict[str, str]]] = None,
    focus_tables: Optional[List[str]] = None,
) -> List[Dict]:
    """Extract typed comparison filters for a query, driven by column types.

    ``column_types`` maps ``{table: {column: raw_sql_type}}``. When omitted every
    column is treated as text (string equality only) — the safe default when types
    aren't available, matching the prior behavior. Returns
    ``[{column, operator, value}, …]`` ready to drop into ``execution_plan["where"]``.

    ``focus_tables`` are the tables the planner already resolved for this query (its
    measure/selected columns). A bare temporal phrase ("revenue last month") names
    no table in the text, so it is attached to *these* tables' date column — which
    is how "last month" reaches ``payments.payment_date`` when the user only said
    "revenue". Falls back to the whole schema when no focus is given.
    """
    column_types = column_types or {}
    strict = bool(column_types)

    col_tables: Dict[str, List[tuple]] = {}
    for table, cols in schema.items():
        for col in cols:
            col_tables.setdefault(col.lower(), []).append((table, col))

    query_lower = query.lower()
    query_words = set(query_lower.split())
    excluded = _GRAMMAR_TOKENS
    table_names = {t.lower() for t in schema}

    filters: List[Dict] = []
    seen: set = set()
    dated_columns: set = set()  # qualified date columns already filtered

    def _record(table, actual, spec) -> None:
        key = (table, actual, spec["operator"])
        if key in seen:
            return
        seen.add(key)
        filters.append(spec)

    # Longest column names first so "payment_method" wins over "method".
    all_cols = col_tables.keys()
    for col_lower in sorted(col_tables, key=len, reverse=True):
        table, actual = _pick_table(col_tables[col_lower], query_lower, query_words)
        category = classify_sql_type((column_types.get(table) or {}).get(actual))
        # Skip columns the query never names. Every handler pattern EXCEPT the
        # boolean one is ``\b{form}\b``-anchored, so a match form absent as a
        # substring of the query can never match — running ~10 regexes for it is
        # pure waste. On a wide ERP schema a question names a handful of the
        # columns, not all of them, so this turns an O(all-columns) scan into
        # O(mentioned-columns), the dominant per-query cost on wide schemas.
        # Booleans are exempt: they also match adjective phrasing ("paid",
        # "unpaid") that does not contain the column name. A substring is a
        # necessary condition for the word-boundary match, so nothing real is
        # dropped.
        if category != "boolean":
            forms = _column_match_forms(col_lower, all_cols)
            if not any(f in query_lower for f in forms):
                continue
        handler = get_type_handler(category)
        name_re = _name_pattern(_column_match_forms(actual, all_cols))
        for spec in handler.extract(query, table, actual, col_lower, name_re, excluded, strict):
            # A captured value equal to a real column/table name isn't a literal
            # (exact match only — never fragment/word-part matching, which would
            # reintroduce the schema-dependent determinism bug).
            val = spec.get("value")
            if isinstance(val, str) and (val.lower() in col_tables or val.lower() in table_names):
                continue
            _record(table, actual, spec)
            if category == "date":
                dated_columns.add(f"{table}.{actual}")

    # Bare calendar expression ("orders this month") not attached to a column:
    # apply it to the SOLE date column of the table(s) the query names. Skip if
    # ambiguous (more than one candidate) — never guess which date column.
    cal = re.search(rf"\b({_TEMPORAL_PHRASE})\b", query_lower)
    if cal and not dated_columns:
        expr = re.sub(r"\s+", " ", cal.group(1).strip().lower())
        rng = _temporal_range(expr, _today())
        if rng:
            named_tables = [
                t for t in schema
                if t.lower() in query_lower or t.lower().rstrip("s") in query_words
            ]
            # A table the text names wins; otherwise fall back to the tables the
            # planner already resolved (the measure's table for "revenue last
            # month"); only then the whole schema. Narrowing the scope is what
            # makes a single date column resolvable instead of ambiguous.
            focus = [t for t in (focus_tables or []) if t in schema]
            scope = named_tables or focus or list(schema)

            # A column is temporal by declared type OR by naming convention. The
            # second clause matters because SQLite (and any loosely-typed source)
            # stores dates as TEXT — `order_date TEXT`, `created_at TEXT` — so a
            # type-only check would never see them. ISO-8601 text compares
            # correctly with the >= / < bounds emitted below, so the range is right
            # for both real DATE columns and ISO-text ones. Name convention only,
            # no per-schema column list — matches ``semantic_roles`` temporal role.
            def _looks_temporal(t: str, c: str) -> bool:
                category = classify_sql_type((column_types.get(t) or {}).get(c))
                if category == "date":
                    return True
                # Name convention only applies when we actually have type info to
                # work from (``strict``). With no types at all the whole extractor
                # falls back to string-equality; inferring dates from names there
                # would silently change that documented safe default.
                if not strict:
                    return False
                # ...and only for a column the DB does NOT type as numeric/boolean.
                # The name branch exists for ISO dates stored as TEXT (SQLite), which
                # classify as "string". A REAL column literally named ``time`` (a
                # duration in seconds) or ``date`` (a serial number) is numeric, not
                # temporal — applying an ISO-date range to it compares a float to
                # date strings and silently returns the wrong rows.
                if category != "string":
                    return False
                cl = c.lower()
                return cl.endswith(("_at", "_date", "_time")) or cl in (
                    "date", "time", "timestamp", "datetime",
                )

            date_cols = [
                (t, c) for t in scope for c in schema.get(t, []) if _looks_temporal(t, c)
            ]
            if len(date_cols) == 1:
                t, c = date_cols[0]
                start, end = rng
                _record(t, c, {"column": f"{t}.{c}", "operator": ">=", "value": start.isoformat()})
                _record(t, c, {"column": f"{t}.{c}", "operator": "<", "value": end.isoformat()})

    return filters
