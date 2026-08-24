"""Intent Builder for structured query interpretation.

This module converts semantic matches into structured intent to make the compiler
deterministic rather than a guessing engine.

Embedding matching system upgrade.
Fixes: Multi-column extraction, filter binding, intent contract enforcement.
"""

import re
from typing import Dict, List, Optional, Any

from dbbuddy_core.logger import get_logger
from dbbuddy_core.semantic_roles import is_identifier_name

logger = get_logger()


# Aggregation keyword mapping
AGGREGATION_KEYWORDS = {
    "sum": "SUM",
    "total": "SUM",
    "count": "COUNT",
    "number": "COUNT",
    "avg": "AVG",
    "average": "AVG",
    "max": "MAX",
    "min": "MIN"
}

# Multi-word aggregation phrases — the single-word keyword scan can't see these
# ("how many orders" contains no standalone AGGREGATION_KEYWORDS word).
AGGREGATION_PHRASES = {
    "how many": "COUNT",
    "number of": "COUNT",
}

# Ranking keywords for ORDER BY detection
RANKING_KEYWORDS = {
    "top": "DESC",
    "highest": "DESC",
    "largest": "DESC",
    "most": "DESC",
    "bottom": "ASC",
    "lowest": "ASC",
    "smallest": "ASC"
}


# "top 5 nations by number of customers": in a ranking, "by" introduces the
# *measure* and the thing being ranked comes before it — the opposite of "total
# revenue by nation". Read the wrong way round, the plan groups by the measure
# column and aggregates the dimension: SUM over a text column, ranked.
_RANKING_DIMENSION = re.compile(r"\b(?:top|bottom)\s+\d+\s+(.+?)\s+by\s+")


def ranking_dimension_phrase(query: str) -> Optional[str]:
    """The phrase a "top N … by …" question ranks, or None if it is not one."""
    match = _RANKING_DIMENSION.search(query.lower())
    return match.group(1).strip() if match else None


def extract_grouping_table(query: str, tables: List[str],
                           schema: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """Extract grouping table from query patterns like "per X" or implicit grouping.

    FIX: Detect grouping table for COUNT optimization.
    FIX: Detect implicit grouping patterns like "users with more than X".
    FIX: Entity table priority - users/customers over subscriptions/payments.

    Args:
        query: User's natural language query
        tables: List of tables involved in query

    Returns:
        Table name for grouping, or None
    """
    query_lower = query.lower()

    # Explicit "per/by/for each <entity>" — match the entity word against the
    # actual tables in play (singular or plural), so it works on any schema. In a
    # ranking the roles are reversed and the ranked entity comes first.
    ranked = ranking_dimension_phrase(query_lower)
    per_match = (re.search(r"([a-z_]+)\s*$", ranked) if ranked
                 else re.search(r"\b(?:per|by|for each|each)\s+([a-z_]+)", query_lower))
    if per_match:
        entity = per_match.group(1)
        # Search the whole schema, not only the tables detected so far. The
        # dimension named after "per" is frequently a table the question never
        # mentions otherwise ("how many suppliers per nation" names `supplier`
        # only), and when it was missing from `tables` this fell through to the
        # implicit branch and returned the *fact* table — inverting the query.
        # The plan then grouped by the supplier key and counted nations: one row
        # per supplier, every count 1, and a total that no longer matches the
        # ungrouped answer.
        candidates = list(tables or []) + [t for t in (schema or {})
                                           if t not in (tables or [])]
        for table in candidates:
            tl = table.lower()
            if entity == tl or entity == tl.rstrip("s") or entity.rstrip("s") == tl.rstrip("s"):
                return table

    # Implicit grouping: the first table the query names directly (by name,
    # singular or plural). No hardcoded entity list — grouping vs. event-table
    # disambiguation for COUNT is handled separately by get_count_target_table.
    for table in tables:
        tl = table.lower()
        if tl in query_lower or tl.rstrip("s") in query_lower.split():
            return table

    return None


# "per X", "by X", "for each X" — the phrases that ask for a grain. Ranking uses
# "by" too ("top 5 customers by revenue"), which is harmless here: that plan does
# group, so nothing is reported missing.
_GROUPING_PHRASE = re.compile(r"\b(?:per|by|for each|each)\s+([a-z_]+)", re.IGNORECASE)


def requests_grouping(query: str) -> bool:
    """Whether the question asked for a grain, regardless of whether one resolved.

    Recorded on the intent so the confidence check can tell "grouped by the wrong
    thing" (an ambiguity) apart from "asked to group and did not" (a dropped
    clause). Without it, a plan that lost its GROUP BY was indistinguishable from
    one that was never asked for a grain, and both reported high confidence.
    """
    return bool(_GROUPING_PHRASE.search(query or ""))


def strip_measure_only_select(kept: List[Dict], aggregation: Optional[Dict],
                             requested_grouping: bool) -> List[Dict]:
    """Drop the aggregation's own column from a kept-dimension list.

    The measure is never the grain. This matters because the semantic enhancer
    applies a learned mapping by *appending the column reference* to the query
    text: "total amount by region" becomes "total amount by region
    payments.amount payments". That is indistinguishable from the user naming the
    column, so retrieval returns it, it survives into the kept list, and the
    branch that binds the actual dimension is skipped because the list already
    looks populated. The plan ends up with an aggregate and no grouping — a
    single global total answering a "by region" question, and an instance that
    had learned something answering worse than a cold one.

    Gated on the question actually asking for a grain. Without that gate, "total
    amount" would have its select cleared and the dimension branch would go
    looking for a grouping nobody requested.
    """
    if not kept or not requested_grouping:
        return kept
    agg_col = (aggregation or {}).get("column") or {}
    agg_ref = (agg_col.get("table"), agg_col.get("column"))
    if not all(agg_ref):
        return kept
    return [c for c in kept
            if (c.get("table"), c.get("column")) != agg_ref]


def prefer_label_over_key(kept: List[Dict], schema: Dict[str, List[str]],
                          column_roles: Optional[Dict[str, Dict[str, str]]] = None) -> List[Dict]:
    """Swap a grouping key for its table's human label, when there is one.

    ``grouping_column_refs`` yields ``regions.region_id`` before
    ``regions.region_name`` — both match the token "region" — and the filter that
    picks the grain takes whichever arrives first. Grouping on the key answers at
    the right grain with the wrong label: eight rows of integers, and a
    near-identical question ("total amount by region") answering with names
    instead.

    The dimension-binding branch further down already resolves the label via
    ``_find_identifier_column``; it just never runs once the select list is
    non-empty. This applies the same preference to a grain that is already chosen.

    A table with nothing but a key keeps its key — that is the honest answer for a
    table that has no label to offer.
    """
    if not kept:
        return kept
    out: List[Dict] = []
    seen = set()
    for col in kept:
        table, column = col.get("table"), col.get("column")
        if table and column and is_identifier_name(column):
            label = _find_identifier_column(table, schema, column_roles)
            if label and label != column:
                col = {**col, "column": label, "alias": label}
        ref = (col.get("table"), col.get("column"))
        if ref in seen:
            continue
        seen.add(ref)
        out.append(col)
    return out


# The words a grouping phrase can introduce, and the trailing words that end it.
# "by region last quarter" groups by region, not by "quarter".
# A qualified reference ("payments.amount") terminates the phrase. The semantic
# enhancer appends learned mappings to the query text, and those are never part of
# what the user asked to group by. Without that terminator the phrase failed to
# match at all on any instance that had learned something — so the narrowing below
# silently stopped working exactly where the enhancer had already made the grain
# harder to find.
_GROUPING_PHRASE_TAIL = re.compile(
    r"\b(?:per|by|for each|each)\s+([a-z_]+(?:\s+[a-z_]+)*?)"
    r"(?=\s+(?:last|this|next|past|over|since|between|from|where|in|during|for)\b"
    r"|\s+\S*\."
    r"|\s*$)",
    re.IGNORECASE,
)


def narrow_to_phrase_head(kept: List[Dict], query: str) -> List[Dict]:
    """Keep the candidate dimension that matches the head of the grouping phrase.

    "total amount by product category" grouped by ``product_name``, ``category``
    *and* ``product_id`` — 60 rows, one per product, for a question that asked for
    five categories. Both ``product_name`` and ``category`` match a token in
    "product category", and the filter that picks the grain keeps every match.

    English puts the head noun last: "product category" is a kind of category, not
    a kind of product. So the last word of the phrase decides.

    Conservative on purpose. If narrowing would leave nothing, the original set is
    returned — "top 5 customers by total payment amount" has a head ("amount")
    that belongs to the measure, and stripping the real grain there would be worse
    than keeping an extra column.
    """
    if not kept or len(kept) < 2:
        return kept
    match = _GROUPING_PHRASE_TAIL.search(query or "")
    if not match:
        return kept
    words = [w for w in match.group(1).lower().split() if w]
    if len(words) < 2:
        return kept
    head = words[-1]

    narrowed = [
        c for c in kept
        if head in str(c.get("column", "")).lower().split("_")
    ]
    return narrowed or kept


# Aggregates that require a number. COUNT counts rows of anything; MIN/MAX are
# defined for text and dates too ("earliest order", "last name alphabetically").
_NUMERIC_ONLY_AGGREGATES = {"SUM", "AVG"}


def aggregation_is_type_safe(aggregation: Optional[Dict],
                             column_types: Optional[Dict[str, Dict[str, str]]]) -> bool:
    """Whether this aggregate can legally be applied to the column it names.

    The `legacy` dataset has a table `Customer` with a text column also called
    `Customer`. Asked for "total customer lifetime value", the engine matched the
    word and compiled ``SUM("Customer"."Customer")``. PostgreSQL rejects it —
    ``function sum(text) does not exist`` — while SQLite coerces text to a number
    and returns 0.0, so the defect was invisible for as long as the correctness
    suites ran on SQLite alone.

    Returning False here means no aggregation resolves, which is the honest
    outcome: the question still carries an aggregation signal the plan does not
    satisfy, so the dropped-clause check lowers confidence and the user is told
    the engine did not understand, rather than handed a zero that looks like an
    answer.

    Permissive when the type is unknown — refusing there would turn "we don't
    know" into "no" and disable aggregation for every caller with no column types,
    which is the same safe default the rest of the extractor documents.
    """
    if not isinstance(aggregation, dict):
        return True
    function = str(aggregation.get("function") or "").upper()
    if function not in _NUMERIC_ONLY_AGGREGATES:
        return True

    column = aggregation.get("column")
    if not isinstance(column, dict):
        return True
    table, name = column.get("table"), column.get("column")
    if not table or not name or not column_types:
        return True

    declared = (column_types.get(table) or {}).get(name)
    if not declared:
        return True
    from dbbuddy_core.type_handlers import classify_sql_type

    return classify_sql_type(declared) == "numeric"


def has_aggregation_signal(query: str) -> bool:
    """Whether the question asks for an aggregate at all.

    Gates the widening below. Without a gate, a plain browse query would grow a
    SUM over whatever numeric column the schema happens to contain.
    """
    lowered = (query or "").lower()
    return (
        any(w in AGGREGATION_KEYWORDS for w in lowered.split())
        or any(frag in lowered for frag in _MEASURE_FRAGMENTS)
        # "how many" / "how much" request a count or a total without using any
        # of the keywords above. Checked here rather than added to
        # AGGREGATION_KEYWORDS, which is a set of single words shared with other
        # call sites that expect exactly that.
        or "how many" in lowered
        or "how much" in lowered
    )


def aggregation_with_widening(query: str, columns: List[Dict], schema: Dict[str, List[str]],
                              tables: List[str],
                              column_types: Optional[Dict[str, Dict[str, str]]] = None,
                              primary_keys: Optional[Dict[str, List[str]]] = None,
                              ties_out: Optional[List[str]] = None) -> Optional[Dict]:
    """Resolve the aggregation, falling back to the whole schema if need be.

    A grouping phrase does not change what is being totalled, but it does change
    what retrieval returns. On a real schema, "total revenue" put the measure at
    rank 2 while "total revenue by region" pushed it to rank 8 behind the
    dimension's own columns — table detection then kept only the dimension table,
    and a measure search confined to it found nothing. The question plainly asked
    for a total and the plan carried none.

    Worse, the *intent* recorded no aggregation either, so nothing downstream
    could tell that one had gone missing: the query answered a different question
    at full confidence.

    The engine already solves the mirror image of this twice —
    ``find_numeric_column(widen=True)`` and ``extract_grouping_table``, which
    searches the whole schema because the dimension is "frequently a table the
    question never mentions otherwise". A measure is no less frequently so.

    Detected tables stay first in the widened list, so this only ever adds
    candidates; it never overrides a measure that the detected tables could
    already supply.
    """
    aggregation = extract_aggregation(query, columns, schema, tables,
                                      column_types, primary_keys, ties_out)
    if aggregation or not has_aggregation_signal(query):
        return aggregation

    widened = list(tables or []) + [t for t in (schema or {}) if t not in (tables or [])]
    if len(widened) == len(tables or []):
        return aggregation   # nothing new to look at

    return extract_aggregation(query, columns, schema, widened,
                               column_types, primary_keys, ties_out)

def extract_ranking_and_limit(query: str) -> tuple[Optional[Dict], Optional[int]]:
    """Extract ranking (ORDER BY) and limit intent from query.

    Detect ranking keywords and limit numbers.

    Args:
        query: User's natural language query

    Returns:
        Tuple of (order_by_dict, limit_int) or (None, None)
    """
    query_lower = query.lower()

    limit = None
    order = None

    # Detect "top 5", "top 10", etc.
    match = re.search(r"(top|bottom)\s+(\d+)", query_lower)
    if match:
        keyword, num = match.groups()
        limit = int(num)
        direction = RANKING_KEYWORDS.get(keyword, "DESC")

        order = {
            "type": "aggregation",  # will sort by metric
            "direction": direction
        }

    # Detect "highest", "lowest", "largest", "smallest", "most"
    for word, direction in RANKING_KEYWORDS.items():
        if word in query_lower:
            order = {
                "type": "aggregation",
                "direction": direction
            }
            break

    return order, limit


def infer_default_metric_for_top_query(query: str, schema: Dict[str, List[str]], tables: List[str]) -> Optional[Dict]:
    """Infer default metric for "top" queries when no aggregation is specified.

    Fix "fake-right" top users logic with metric priority system.
    Instead of COUNT(users.id) which always equals 1, infer a meaningful metric.

    Metric Priority System for consistent behavior.
    Priority order: revenue > amount > price > value > total > cost > orders > count
    This ensures consistent metric selection across schemas.

    Args:
        query: User's natural language query
        schema: Database schema
        tables: List of tables involved in query

    Returns:
        Dict with function and column, or None if not a top query
    """
    query_lower = query.lower()

    # Check if this is a ranking query
    has_ranking = any(word in query_lower for word in RANKING_KEYWORDS.keys())
    if not has_ranking:
        return None

    # Metric Priority System
    # Priority order for measure columns (highest to lowest)
    MEASURE_PRIORITY = [
        "revenue",
        "amount",
        "price",
        "value",
        "total",
        "cost",
        "quantity",
        "count"
    ]

    # Search all tables for priority metrics in order
    for priority_metric in MEASURE_PRIORITY:
        for table in tables:
            if table in schema:
                for col in schema[table]:
                    if col.lower() == priority_metric:
                        # Determine aggregation function based on metric type
                        if priority_metric in ["revenue", "amount", "price", "value", "total", "cost"]:
                            func = "SUM"
                        elif priority_metric in ["quantity", "count"]:
                            func = "COUNT"
                        else:
                            func = "SUM"  # Default to SUM for unknown metrics

                        logger.debug(f"INFERRED DEFAULT METRIC (priority {priority_metric}): {func}({table}.{col})")
                        return {
                            "function": func,
                            "column": {
                                "table": table,
                                "column": col,
                                "alias": col,
                                "aggregation": None
                            }
                        }

    # Fallback: COUNT(*) using first table's id
    if tables and schema.get(tables[0]):
        for col in schema[tables[0]]:
            if col == "id":
                logger.debug(f"INFERRED DEFAULT METRIC (fallback): COUNT({tables[0]}.id)")
                return {
                    "function": "COUNT",
                    "column": {
                        "table": tables[0],
                        "column": col,
                        "alias": col,
                        "aggregation": None
                    }
                }

    return None


def extract_having(query: str) -> Optional[Dict]:
    """Extract HAVING clause intent from query.

    Detect filtering on aggregated values.

    Args:
        query: User's natural language query

    Returns:
        Dict with operator and value, or None
    """
    query_lower = query.lower()

    # Detect "more than", "greater than", "over"
    match = re.search(r"(more than|greater than|over)\s+(\d+)", query_lower)
    if match:
        _, value = match.groups()

        return {
            "operator": ">",
            "value": int(value)
        }

    return None


def find_id_column(schema: Dict[str, List[str]], tables: List[str],
                   grouping_table: Optional[str] = None,
                   primary_keys: Optional[Dict[str, List[str]]] = None) -> Optional[Dict]:
    """Find id column from schema for COUNT aggregation.

    FIX: Use schema-level columns, not selected columns.
    FIX: Prefer non-grouping table for COUNT.
    Aggregation must select its own column from schema.

    Args:
        schema: Database schema
        tables: List of tables involved in query
        grouping_table: Table used for grouping (to avoid for COUNT)

    Returns:
        Column dict for id, or None
    """
    # Prefer a non-grouping table: "how many suppliers per region" counts
    # suppliers, and the region is only the grain.
    ordered = ([t for t in tables if t != grouping_table] + [grouping_table]
               if grouping_table else list(tables))
    for table in ordered:
        if not table:
            continue
        anchor = _count_anchor_column(table, schema, primary_keys)
        if anchor:
            return {"table": table, "column": anchor, "alias": anchor,
                    "aggregation": None}
    return None


def _count_anchor_column(table: str, schema: Dict[str, List[str]],
                         primary_keys: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """The column to hang a COUNT on for ``table``, or None if it has none.

    A literal ``id`` first, then any name the identifier check recognises,
    including the ``*key`` form a warehouse schema uses (``s_suppkey``), then the
    table's first column — which in every convention that has no surrogate key is
    still the key.

    Requiring a literal ``id`` was the bug: TPC-H has no column named ``id``
    anywhere, so this returned None for every table, the caller fell back to
    "first column of the first detected table", and "how many suppliers per
    region" counted *regions* — five rows where the answer was five hundred.
    """
    columns = schema.get(table) or []
    if not columns:
        return None
    # The declared primary key first. A COUNT over any other column counts only
    # its non-NULL rows, and a warehouse fact's leading columns are *nullable*
    # foreign keys — counting `store_sales` on `ss_sold_date_sk` quietly returned
    # 117,632 of 120,000 rows. The key cannot be null by definition, so it is the
    # only anchor that answers "how many rows".
    declared = [c for c in (primary_keys or {}).get(table, []) if c in columns]
    if declared:
        return declared[0]
    prefix = uniform_column_prefix(columns)
    for col in columns:
        if col.lower() == "id":
            return col
    for col in columns:
        if is_identifier_name(col):
            return col
    for col in columns:
        tail = "".join(column_match_tokens(col, prefix))
        if prefix and tail.endswith("key"):
            return col
    return columns[0]


# Measure-name fragments in priority order. Matched as substrings so real-world
# names like "total_amount", "unit_price", "order_total", "discount_amount" are
# recognised — not just the exact words.
_MEASURE_FRAGMENTS = ("revenue", "sales", "amount", "price", "total", "value", "cost", "subtotal")


_NUMERIC_TYPE_FRAGMENTS = ("int", "decimal", "numeric", "float", "double", "real",
                           "money", "number", "bigint", "smallint", "serial")


def _looks_numeric(sql_type: str) -> bool:
    t = (sql_type or "").lower()
    return any(frag in t for frag in _NUMERIC_TYPE_FRAGMENTS)


def split_identifier(name: str) -> list:
    """Lowercase word-parts of a column/table name, across naming conventions.

    Splits on underscores **and** camelCase/PascalCase boundaries and digit runs,
    so ``ListPrice`` → ``[list, price]``, ``TaxAmt`` → ``[tax, amt]``,
    ``SalesOrderID`` → ``[sales, order, id]``, ``unit_price`` → ``[unit, price]``.
    Without the camelCase split, a SQL-Server/.NET schema (AdventureWorks) is
    opaque to any word-level match — the user says "list price", the column is
    ``ListPrice``, and the two never meet.
    """
    if not name:
        return []
    # Order matters: an acronym run before a Capitalized word ("XMLParser" →
    # XML, Parser), then a Capitalized word, then a trailing acronym ("ID"), then
    # lowercase runs and digits. Without the acronym cases, "SalesOrderID" split
    # its "ID" into two single letters.
    parts = re.findall(
        r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[A-Z]+|[a-z]+|[0-9]+",
        re.sub(r"_", " ", name))
    return [p.lower() for p in parts if p]


def singular_forms(word: str) -> set:
    """A typed word and the singulars a schema might spell it with.

    Plural forms a user says but a column or table name never carries: "order
    priorities" is ``o_orderpriority``, "customer addresses" is
    ``customer_address``. Without the -ies/-es rules the phrase matched nothing
    and the question silently bound to a shorter table or lost its breakdown.
    """
    out = {word, word.rstrip("s")}
    if word.endswith("ies"):
        out.add(word[:-3] + "y")
    if word.endswith("es"):
        out.add(word[:-2])
    return out


def expand_query_tokens(query: str) -> set:
    """Query word tokens, their singular forms, and adjacent-word concatenations.

    A user types words with spaces; a schema may spell the same thing with no
    separator at all — "extended price" is ``l_extendedprice``, "ship mode" is
    ``l_shipmode``, "market segment" is ``c_mktsegment``. Splitting the *column*
    cannot recover that boundary: ``extendedprice`` is one token however it is
    split. So the join has to happen on the query side, by re-concatenating
    adjacent words. ``detect_tables_from_query`` already does exactly this for
    table names; measures, dimensions and grouping phrases need it too, or a
    warehouse schema (TPC-H, and the mainframe-derived schemas that prefix and
    concatenate every column) is unreachable by name and the engine silently
    falls back to whichever numeric column it finds first.
    """
    forms = singular_forms
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9_]+", query)]
    tokens = set()
    for w in words:
        tokens |= forms(w)
    for size in (2, 3):
        for i in range(len(words) - size + 1):
            group = words[i:i + size]
            # Singularise only the last word of a phrase: "order priorities"
            # concatenates to ``orderpriority``, not ``orderprioritie``.
            for last in forms(group[-1]):
                tokens.add("".join(group[:-1]) + last)
    return tokens


def uniform_column_prefix(columns: List[str]) -> Optional[str]:
    """The short token that *every* column of a table begins with, if any.

    Warehouse and mainframe schemas prefix each column with an abbreviation of
    its table: TPC-H writes ``l_orderkey``/``l_quantity`` on ``lineitem`` and
    ``ps_supplycost`` on ``partsupp``. Nobody speaks that prefix, but it is a
    full token, so it halves every match score and defeats the "every part of the
    name was named" test that a two-word column otherwise wins on.

    Derived from the table's own columns — no list of known prefixes. Two guards
    keep it from eating meaningful words: the token must be shared by *all* the
    table's columns (a table with `order_date`/`order_status`/`customer_id` has
    no uniform prefix), and it must be at most three characters, so a repeated
    real word like ``order`` survives and stays matchable.
    """
    if len(columns) < 2:
        return None
    prefix = None
    for col in columns:
        parts = split_identifier(col)
        if len(parts) < 2 or len(parts[0]) > 3:
            return None
        if prefix is None:
            prefix = parts[0]
        elif parts[0] != prefix:
            return None
    return prefix


def column_match_tokens(column: str, prefix: Optional[str] = None) -> List[str]:
    """The tokens of a column name that a user would actually say."""
    parts = split_identifier(column)
    if prefix and len(parts) > 1 and parts[0] == prefix:
        return parts[1:]
    return parts


def segment_token(token: str, vocab: set, min_piece: int = 3) -> Optional[List[str]]:
    """Split a run-together column token into words the *query* used, or None.

    ``o_totalprice`` is one token however it is split, but "total order price"
    contains both halves of it — just not next to each other, so re-joining
    adjacent query words cannot reach it. Segmenting the column token against the
    query's own words as the dictionary does, and needs no word list of any kind:
    the only vocabulary is what the user typed, so a schema-specific abbreviation
    (``mktsegment``, ``acctbal``) correctly fails to match rather than being
    guessed at.

    Pieces are at least three characters so single letters and stray fragments
    cannot stitch an arbitrary token together.
    """
    n = len(token)
    if n < min_piece * 2:
        return None
    # Shortest-path over split points: back[j] is the start of the piece ending
    # at j on some valid segmentation.
    back: List[Optional[int]] = [None] * (n + 1)
    reach = [False] * (n + 1)
    reach[0] = True
    for i in range(n):
        if not reach[i]:
            continue
        for j in range(i + min_piece, n + 1):
            if not reach[j] and token[i:j] in vocab:
                reach[j] = True
                back[j] = i
    if not reach[n]:
        return None
    pieces, cursor = [], n
    while cursor:
        start = back[cursor]
        if start is None:
            return None
        pieces.append(token[start:cursor])
        cursor = start
    pieces.reverse()
    return pieces if len(pieces) > 1 else None


def column_token_named(token: str, query_tokens: set) -> bool:
    """Whether the query names one token of a column name."""
    if token in query_tokens or token.rstrip("s") in query_tokens:
        return True
    return segment_token(token, query_tokens) is not None


def tokens_present(parts: List[str], query_tokens: set) -> int:
    """How many of a column's tokens the query names (singular-insensitive)."""
    return sum(1 for p in parts if column_token_named(p, query_tokens))


# Word endings that mark a column as a label, timestamp or key rather than a
# measure, applied to a *concatenated* name where no separator marks the
# boundary (``l_shipdate``, ``c_custkey``). Only consulted for tables that
# demonstrably use the concatenating convention — see ``_is_plausible_measure``.
_NON_MEASURE_ENDINGS = ("date", "time", "status", "type", "code", "name",
                        "email", "key", "flag", "number")


def find_numeric_column(schema: Dict[str, List[str]], tables: List[str],
                        column_types: Optional[Dict[str, Dict[str, str]]] = None,
                        widen: bool = True) -> Optional[Dict]:
    """Find the best measure column for SUM/AVG aggregation.

    FIX: Use schema-level columns, not selected columns.
    FIX: Match measure names as substrings ("total_amount" → amount), and prefer
         the involved tables but fall back to the whole schema — a query like
         "revenue per user" names only ``users`` while the measure lives in
         ``orders``.

    Args:
        schema: Database schema
        tables: List of tables involved in query
        column_types: {table: {col: sql_type}} — when available, a name-fragment
            match must also be a numeric column, so a *text* code that merely
            contains a measure word (``SalesOrderNumber``, ``AccountNumber``) is
            never summed. A name alone cannot tell a measure from a code.

    Returns:
        Column dict for the chosen measure column, or None
    """
    def _is_numeric(table: str, col: str) -> bool:
        if not column_types:
            return True  # no type info — fall back to name-only matching
        sql_type = (column_types.get(table) or {}).get(col, "")
        return _looks_numeric(sql_type)

    def _best_in(table_list: List[str]) -> Optional[Dict]:
        for fragment in _MEASURE_FRAGMENTS:
            for table in table_list:
                columns = schema.get(table, [])
                prefix = uniform_column_prefix(columns)
                for col in columns:
                    cl = col.lower()
                    # identifiers/foreign keys are not measures — including the
                    # ``*key`` form a warehouse schema uses instead of ``*_id``
                    if not _is_plausible_measure(col, prefix):
                        continue
                    if fragment in cl and _is_numeric(table, col):
                        return {
                            "table": table,
                            "column": col,
                            "alias": col,
                            "aggregation": None,
                        }
        return None

    # Prefer a measure column from the tables already in play.
    found = _best_in(tables)
    if found:
        return found

    # Otherwise look across the rest of the schema (the measure may live in a
    # table the query only referenced implicitly, e.g. orders for "revenue").
    if not widen:
        # The question named its tables and no column in them carries a measure
        # name. Reaching into an unrelated table then answers a question nobody
        # asked, with a number that is not even wrong: "total account balance of
        # customers" became SUM(lineitem.l_extendedprice), and the grouped form
        # of it joined customer → supplier → lineitem, inflating the guess 40x.
        # Widening is only justified when the query named no table to anchor to.
        return None
    return _best_in([t for t in schema.keys() if t not in tables])


# Words that introduce a grouping dimension. Everything after one of these, up to
# the next clause word, names how the answer should be broken down — not what is
# being measured.
_GROUPING_MARKERS = ("by", "per", "for each", "grouped by", "broken down by")

# Columns that are never a sensible SUM/AVG target regardless of naming.
_NON_MEASURE_SUFFIXES = ("_id", "_at", "_on", "_date", "_time", "_status", "_type",
                         "_code", "_name", "_email", "_by")
_NON_MEASURE_NAMES = {"id", "pk", "status", "state", "type", "category", "name",
                      "email", "date", "time", "created_at", "updated_at", "label",
                      "segment", "department", "country", "city", "method",
                      "postcode", "description", "comment", "note"}


def _grouping_columns(query: str, schema: Dict[str, List[str]],
                      tables: List[str]) -> set:
    """Column names the query asks to group *by*, lowercased.

    Text after "by"/"per" is the dimension. Returning it lets the measure search
    exclude it, which is the difference between ``SUM(orders.freight) GROUP BY
    status`` and the nonsensical ``SUM(orders.status)``.
    """
    lowered = query.lower()
    # A ranking names its dimension *before* "by" ("top 3 ship modes by total
    # extended price"). Taking the text after "by" as the dimension made the
    # measure the grain and the dimension the measure — SUM over the ship-mode
    # text column, grouped by every distinct price.
    tail = ranking_dimension_phrase(lowered) or ""
    if not tail:
        for marker in _GROUPING_MARKERS:
            idx = lowered.find(f" {marker} ")
            if idx != -1:
                tail = lowered[idx + len(marker) + 2:]
                break
    if not tail:
        return set()

    # Concatenations included: "by ship mode" names ``l_shipmode``, which no
    # amount of splitting the column can reach.
    return {col.lower() for _, col in grouping_column_refs(query, schema, tables)}


def grouping_column_refs(query: str, schema: Dict[str, List[str]],
                         tables: List[str]) -> List[tuple]:
    """``(table, column)`` for every column the grouping phrase names, in order.

    The names alone are enough to *exclude* a dimension from the measure search,
    but binding one into SELECT needs its table too — and it has to come from the
    schema rather than from the retrieved context, because retrieval returns a
    top-k slice: "total quantity per return flag" simply did not have
    ``l_returnflag`` in its slice, so the grouping phrase was read, understood,
    and then silently dropped for want of a column to attach it to.
    """
    lowered = query.lower()
    tail = ranking_dimension_phrase(lowered) or ""
    if not tail:
        for marker in _GROUPING_MARKERS:
            idx = lowered.find(f" {marker} ")
            if idx != -1:
                tail = lowered[idx + len(marker) + 2:]
                break
    if not tail:
        return []

    tokens = expand_query_tokens(tail)
    scored = []
    candidate_tables = list(tables or []) + [t for t in schema
                                             if t not in (tables or [])]
    for rank, table in enumerate(candidate_tables):
        columns = schema.get(table, [])
        prefix = uniform_column_prefix(columns)
        for col in columns:
            cl = col.lower()
            parts = column_match_tokens(col, prefix)
            present = tokens_present(parts, tokens)
            if cl not in tokens and not present:
                continue
            # Rank by how much of the phrase the column accounts for. A schema
            # with role-playing dimensions has several columns sharing a word —
            # ``ws_sold_date_sk`` and ``ws_ship_date_sk`` both match "date" — and
            # returning them in schema order answered a question about shipping
            # with the sale date. Neither name is more correct in general; the
            # one the user named more of is.
            scored.append(((1 if cl in tokens else 0, present,
                            present == len(parts), -rank), table, col))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(table, col) for _, table, col in scored]


def _is_plausible_measure(column: str, prefix: Optional[str] = None) -> bool:
    """Whether a column could be the thing a SUM/AVG is over.

    ``prefix`` is the table's uniform column prefix when it has one. Its presence
    is evidence that this schema concatenates words inside a column name, and
    that is the only case where the separator-free ending test below is safe to
    run: ``c_custkey`` is a key and ``l_shipdate`` is a date, but on a schema
    that does use separators, ``update`` merely ends in "date" and a monkey is
    not a key.
    """
    cl = column.lower()
    if cl in _NON_MEASURE_NAMES or is_identifier_name(column):
        return False
    if cl.endswith(_NON_MEASURE_SUFFIXES):
        return False
    if prefix:
        tail = "".join(column_match_tokens(column, prefix))
        if tail.endswith(_NON_MEASURE_ENDINGS):
            return False
    return True


def _match_measure_column(query_tokens: set, grouping_cols: set,
                          schema: Dict[str, List[str]],
                          tables: List[str],
                          ties_out: Optional[List[str]] = None) -> Optional[Dict]:
    """The column the query names as its measure, or None.

    Searches the query's own tables first, then the rest of the schema. The
    widening matters: "total credit limit" names no table, so table detection
    returned whatever it could and the measure was hunted only there — producing
    ``SUM(payments.amount)`` while ``customers.credit_limit`` sat unexamined. A
    column the user named by its real words should be found wherever it lives.

    Exact whole-column matches win over part matches, so ``credit_limit`` beats a
    table that merely has a ``credit`` column.
    """
    ordered = list(tables or []) + [t for t in schema if t not in (tables or [])]

    # Score each plausible measure by how much of its name the query names, then
    # take the best. Scoring (not first-match) is what lets "average list price"
    # pick ``ListPrice`` (both parts named) over ``SalesQuota`` (a numeric column
    # that merely shares the generic "sales" measure word) on a wide schema where
    # many columns are measure-ish. camelCase is split so the parts are visible.
    best = None
    best_score = None
    scored: List[tuple] = []
    for rank, table in enumerate(ordered):
        columns = schema.get(table, [])
        # Prefixed schemas name the column ``l_extendedprice``; the prefix is
        # never spoken, so counting it as an unnamed part would score a perfect
        # match at 0.5 and let an unrelated single-word column outrank it.
        prefix = uniform_column_prefix(columns)
        for col in columns:
            cl = col.lower()
            if cl in grouping_cols or not _is_plausible_measure(col, prefix):
                continue
            parts = column_match_tokens(col, prefix)
            if not parts:
                continue
            present = tokens_present(parts, query_tokens)
            if present == 0:
                continue
            # Prefer: whole-name match, then all parts named, then the fraction
            # named, then *how many* words were consumed, then earlier tables
            # (the ones the query actually referenced).
            #
            # The absolute count is what separates two columns that both match
            # completely: "total store sales ext sales price" names every part of
            # both ``ss_sales_price`` and ``ss_ext_sales_price``, and the fraction
            # is 1.0 for each — so the tie fell to schema order and the question
            # was answered from the wrong column, off by the extended amount.
            score = (1 if cl in query_tokens else 0,
                     present == len(parts), present / len(parts), present, -rank)
            scored.append((score[:-1], table, col))
            if best_score is None or score > best_score:
                best = {"table": table, "column": col, "alias": col, "aggregation": None}
                best_score = score

    # Everything that matched the question exactly as well as the winner, in a
    # different table. "total price" describes ``o_totalprice``,
    # ``p_retailprice`` and ``l_extendedprice`` equally; picking one is fine,
    # reporting it as certain is not — the number then flows unqualified into
    # charts and insights. Table order decided the winner, so the tie is the
    # only honest signal that the question had more than one reading.
    if ties_out is not None and best is not None and best_score is not None:
        winner = best_score[:-1]
        ties_out.extend(f"{table}.{col}" for score, table, col in scored
                        if score == winner and table != best["table"])
    return best


_EXTREMUM_WORDS = {"highest": "MAX", "largest": "MAX", "maximum": "MAX", "max": "MAX",
                   "biggest": "MAX", "lowest": "MIN", "smallest": "MIN",
                   "minimum": "MIN", "min": "MIN", "cheapest": "MIN"}


def _extremum_aggregation(query: str, schema: Dict[str, List[str]]) -> Optional[str]:
    """MAX/MIN when the query asks for an extreme *value*, not a ranked list.

    The distinction is what comes after the superlative. "highest unit price"
    names a measure and wants one number; "highest paying customers" and
    "top 5 products by unit price" name an entity and want rows. Ranking keywords
    alone cannot tell them apart, so both used to become a grouped SUM ordered
    descending — an answer of the wrong *shape*, which is easy to mistake for the
    right one at a glance.
    """
    lowered = query.lower()
    words = re.findall(r"[a-z0-9_]+", lowered)
    if not words:
        return None

    # An explicit count ("top 5", "first 3") always means a list.
    if any(w.isdigit() for w in words):
        return None

    table_words = {t.lower() for t in schema} | {t.lower().rstrip("s") for t in schema}
    for i, word in enumerate(words):
        func = _EXTREMUM_WORDS.get(word)
        if not func:
            continue
        rest = words[i + 1:]
        if not rest:
            return None

        # Decide on *order*, not mere presence. "highest paying customers" names
        # the entity first and wants rows; "highest unit price" names the measure
        # first and wants one number.
        #
        # Presence alone was the first attempt and it is not robust: the semantic
        # enhancer appends learned table hints to the end of the query, so
        # "highest unit price" arrives as "highest unit price products" and a
        # presence test sees a table and returns rows. Whatever the user wrote
        # first is what they asked for; trailing hints are annotations.
        column_words = set()
        for cols in schema.values():
            prefix = uniform_column_prefix(cols)
            for col in cols:
                column_words.add(col.lower())
                # underscore + camelCase, minus the table's uniform prefix
                column_words.update(column_match_tokens(col, prefix))

        # Query words that take part in some column name, including as a piece of
        # a run-together one ("retail price" → ``p_retailprice``).
        rest_vocab = expand_query_tokens(" ".join(rest))
        naming_words = set()
        for token in column_words:
            if token in rest_vocab:
                naming_words.add(token)
            pieces = segment_token(token, rest_vocab)
            if pieces:
                naming_words.update(pieces)

        def names_a_column(index: int) -> bool:
            """Whether the word at ``index`` names a column, alone or as part of a
            concatenated one."""
            word = rest[index]
            if word in naming_words or word.rstrip("s") in naming_words:
                return True
            for size in (1, 2, 3):
                joined = "".join(rest[index:index + size])
                if joined in column_words or joined.rstrip("s") in column_words:
                    return True
            return False

        for pos, word in enumerate(rest):
            if names_a_column(pos):
                return func          # measure superlative → single value
            if word in table_words or word.rstrip("s") in table_words:
                # A table noun that *qualifies* a following measure is an
                # adjective, not the thing being ranked: "highest product unit
                # price" wants one number, "highest paying customers" wants rows.
                # Only bail when no column word follows it.
                if any(names_a_column(i) for i in range(pos + 1, len(rest))):
                    continue
                return None          # entity superlative → ranked rows
        return None
    return None


def _counts_a_table(query: str, schema: Dict[str, List[str]]) -> bool:
    """True when a "total/number of X" phrase names a *table*, not a measure.

    "total products" means how many products, but `total` is a SUM keyword, so it
    became ``SUM(products.unit_price)`` — an answer to a question nobody asked,
    and one that looks like a plausible figure rather than an error.
    """
    lowered = query.lower()
    table_words = {t.lower() for t in schema} | {t.lower().rstrip("s") for t in schema}
    for marker in ("total", "number of", "count of"):
        idx = lowered.find(marker)
        if idx == -1:
            continue
        rest = lowered[idx + len(marker):].strip()
        words = re.findall(r"[a-z0-9_]+", rest)
        if not words:
            continue
        head = words[0]
        # Only when the noun *immediately* after the marker is a table: "total
        # revenue by product" must stay a SUM.
        if not (head in table_words or head.rstrip("s") in table_words):
            continue
        # …and only when the table noun is the whole object. "total encounter
        # cost" names a table *qualifying a measure* — it asks for the sum of
        # cost, not for a row count. A trailing column word means the table was
        # an adjective, not the thing being counted.
        column_words = set()
        for cols in schema.values():
            prefix = uniform_column_prefix(cols)
            for col in cols:
                column_words.add(col.lower())
                column_words.update(column_match_tokens(col, prefix))
        # "total order price" names ``o_totalprice`` — the measure word and the
        # table noun are interleaved, so the column is only reachable by
        # segmenting its run-together token against the words of the whole
        # question. What makes the table noun an adjective is that some word
        # *after* it takes part in a column name; that is what is tested here, so
        # "total orders" still counts the table.
        vocab = expand_query_tokens(lowered)
        tail_words = {w for w in words[1:]} | {w.rstrip("s") for w in words[1:]}
        names_measure = False
        for token in column_words:
            if token in tail_words:
                names_measure = True
                break
            pieces = segment_token(token, vocab)
            if pieces and any(p in tail_words for p in pieces):
                names_measure = True
                break
        if names_measure:
            continue
        return True
    return False


def extract_aggregation(query: str, columns: List[Dict], schema: Dict[str, List[str]],
                        tables: List[str],
                        column_types: Optional[Dict[str, Dict[str, str]]] = None,
                        primary_keys: Optional[Dict[str, List[str]]] = None,
                        ties_out: Optional[List[str]] = None) -> Optional[Dict]:
    """Extract aggregation function and target column from query.

    Detect aggregation from natural language.
    FIX: Robust version with null checks.
    FIX: Use schema-level columns, not selected columns.
    FIX: Aggregation selects its own column from schema.
    FIX: Prefer non-grouping table for COUNT.

    Add semantic aggregation inference from learned mappings.
    If no natural language aggregation keyword is found, infer from injected column references.

    Args:
        query: User's natural language query
        columns: List of selected column dicts (for backward compatibility)
        schema: Database schema
        tables: List of tables involved in query

    Returns:
        Dict with function and column, or None if no aggregation detected
    """
    query_words = query.lower().split()

    # Guard against aggregation over-trigger for list/show all queries
    # If query starts with "list" or "show all", disable aggregation
    if query_words[0] in ["list", "show"] and "all" in query_words:
        logger.debug(f"DISABLED AGGREGATION for list/show all query: '{query}'")
        return None

    agg_func = None

    # Detect multi-word aggregation phrases first ("how many", "number of").
    query_text = " ".join(query_words)
    for phrase, func in AGGREGATION_PHRASES.items():
        if phrase in query_text:
            agg_func = func
            break

    # Detect single-word aggregation keyword
    if not agg_func:
        for word in query_words:
            if word in AGGREGATION_KEYWORDS:
                agg_func = AGGREGATION_KEYWORDS[word]
                break

    # "total <table>" counts rows; "total <measure>" sums them. `total` maps to
    # SUM, so without this the former picked an arbitrary numeric column and
    # returned a large, plausible, entirely unrelated number.
    if agg_func == "SUM" and _counts_a_table(query, schema):
        agg_func = "COUNT"
        logger.debug(f"'total <table>' reads as COUNT, not SUM: '{query}'")

    # "highest unit price" asks for one number; "top 5 products by unit price"
    # asks for a ranked list. Both trip the ranking keywords, so the extremum
    # question fell through to SUM plus an ORDER BY — returning 400 grouped rows
    # where the user asked for a single value, which reads as the wrong question
    # having been answered rather than as an error.
    extremum = _extremum_aggregation(query, schema)
    if extremum and not agg_func:
        agg_func = extremum
    elif extremum and agg_func == "SUM":
        agg_func = extremum

    # Semantic aggregation inference from learned mappings
    if not agg_func:
        # Check for injected column references (e.g., payments.amount)
        for word in query_words:
            if "." in word:  # Column reference like payments.amount
                table, col = word.split(".", 1)

                # Heuristic: measure column → SUM
                MEASURE_COLUMNS = {"amount", "price", "value", "total", "cost", "revenue"}
                if col.lower() in MEASURE_COLUMNS:
                    agg_func = "SUM"
                    logger.debug(f"INFERRED AGGREGATION FROM MEMORY: SUM({table}.{col})")

                    # Return the inferred aggregation with the detected column
                    if table in tables:
                        return {
                            "function": agg_func,
                            "column": {
                                "table": table,
                                "column": col,
                                "alias": col,
                                "aggregation": None
                            }
                        }
                    break

    # Detect "per" keyword for grouping + conditional aggregation
    # "per" triggers grouping, but aggregation depends on metric detection
    if "per" in query_words:
        if not agg_func:
            # Only default to COUNT if no metric was detected
            agg_func = "COUNT"
            logger.debug(f"INFERRED COUNT AGGREGATION from 'per' keyword (no metric detected): '{query}'")
        else:
            logger.debug(f"'per' keyword detected with metric {agg_func} - using metric aggregation: '{query}'")

    # Infer default metric for "top" queries when no aggregation detected
    # This fixes the "fake-right" top users logic where COUNT(users.id) always equals 1
    if not agg_func:
        default_metric = infer_default_metric_for_top_query(query, schema, tables)
        if default_metric:
            return default_metric

    if not agg_func:
        return None

    # FIX: Extract grouping table for COUNT optimization
    grouping_table = extract_grouping_table(query, tables, schema)

    # FIX: COUNT should ALWAYS prefer id column from schema
    if agg_func == "COUNT":
        id_col = find_id_column(schema, tables, grouping_table, primary_keys)
        if id_col:
            return {
                "function": "COUNT",
                "column": id_col
            }
        # No anchor at all: only possible when none of the detected tables has a
        # single column, so there is nothing left to count.

    # Prefer a schema column the query *names* ("average age" → AVG(age)) before
    # the measure-name heuristic. find_numeric_column only knows revenue/amount/…
    # fragments, so "age"/"score"/"quantity" otherwise fell through to the id
    # fallback → AVG(id), a wrong answer. Match on the query's word tokens against
    # real column names (skip identifiers, which are never a sensible SUM/AVG).
    query_tokens = expand_query_tokens(query)

    # A column named as the *grouping dimension* is not the measure. "total freight
    # by status" names both `freight` and `status`, and matching on tokens alone
    # picked whichever came first in the schema — yielding SUM(orders.status), a
    # sum over a TEXT column that returns 0 and looks like a data problem rather
    # than a planning one.
    grouping_cols = _grouping_columns(query, schema, tables)

    # ``ties_out`` rather than an extra key in the result: the returned dict is
    # the aggregation contract the planner and the tests compare against, and
    # ambiguity is metadata about the choice, not part of it.
    measure = _match_measure_column(query_tokens, grouping_cols, schema, tables,
                                    ties_out)
    if measure:
        return {"function": agg_func, "column": measure}

    # FIX: Prefer numeric columns from schema for other aggregations
    # This fallback matches on the *column's* name only — it never looks at what
    # the user said — so it may not leave the tables the question named.
    numeric_col = find_numeric_column(schema, tables, column_types,
                                      widen=not tables)
    if numeric_col:
        return {
            "function": agg_func,
            "column": numeric_col
        }

    # Last fallback: any column in the named tables that could carry a measure.
    # It used to be "the first column of the first table", which on a schema
    # whose first column is always its key produced SUM(customer.c_custkey) — a
    # number with no meaning at all, reported like any other total. A key, a
    # label or a timestamp is never the answer to "how much"; when the schema
    # offers no measure, saying nothing is the honest result.
    for table in tables:
        columns = schema.get(table) or []
        prefix = uniform_column_prefix(columns)
        for col in columns:
            if not _is_plausible_measure(col, prefix):
                continue
            if column_types and not _looks_numeric(
                    (column_types.get(table) or {}).get(col, "")):
                continue
            return {"function": agg_func,
                    "column": {"table": table, "column": col, "alias": col,
                               "aggregation": None}}

    return None


def detect_tables_from_query(query: str, schema: Dict[str, List[str]]) -> List[str]:
    """Detect tables directly from query words.

    Detect tables from language, not just schema matches.
    This handles abstract queries like "Show users and their subscriptions".

    Args:
        query: User's natural language query
        schema: Database schema

    Returns:
        List of detected table names
    """
    words = set(query.lower().split())
    detected = []

    # Normalize on *both* sides. Only the table was being singularized, so a
    # singular table met a plural question and matched nothing: against a schema
    # with `patient`, "how many patients" detected no table at all and the whole
    # question was rejected as unrelated to the database. Singular table names are
    # the norm in clinical and warehouse schemas, so this was not an edge case
    # there — it was every question.
    word_forms = set(words)
    for w in words:
        word_forms.add(w.rstrip("s"))
        word_forms.add(w + "s")
        if w.endswith("ies"):
            word_forms.add(w[:-3] + "y")
        if w.endswith("es"):
            word_forms.add(w[:-2])

    # Adjacent words joined with "_", so a snake_case table can be named the way
    # people say it: "encounter diagnosis" → `encounter_diagnosis`,
    # "order items" → `order_items`. Without this a multi-word table was
    # unreachable by name, and the question silently bound to whichever
    # single-word table shared a prefix — here `encounter`, giving a plausible
    # count of the wrong thing.
    ordered = query.lower().split()
    for i in range(len(ordered) - 1):
        pair = f"{ordered[i]}_{ordered[i + 1]}"
        word_forms.add(pair)
        word_forms.add(pair + "s")
        # Singularise the *last* word of the pair, not the joined string:
        # "customer addresses" is ``customer_address``, and stripping the "s"
        # from the whole gives ``customer_addresse``. Without this the question
        # bound to the shorter `customer` table and counted the wrong entity.
        for last in singular_forms(ordered[i + 1]):
            word_forms.add(f"{ordered[i]}_{last}")

    # Adjacent words joined with NO separator, so a camelCase/PascalCase table
    # spoken as separate words is reachable: "sales order header" →
    # `SalesOrderHeader`, "product subcategory" → `ProductSubcategory`. SQL Server
    # / .NET schemas (AdventureWorks) name every multi-word table this way, and
    # without the concatenation the question bound to whichever single-word table
    # shared a prefix (`SalesPerson` for "sales order header") — a wrong table with
    # a plausible count.
    for size in (2, 3, 4):
        for i in range(len(ordered) - size + 1):
            joined = "".join(ordered[i:i + size])
            for form in (joined, joined.rstrip("s"), joined + "s"):
                word_forms.add(form)

    for table in schema.keys():
        tl = table.lower()
        forms = {tl, tl.rstrip("s"), tl + "s"}
        if tl.endswith("y"):
            forms.add(tl[:-1] + "ies")
        if forms & word_forms:
            detected.append(table)

    # A more specific table wins over its parts. "encounter diagnosis" matches
    # `encounter_diagnosis` *and* `encounter`; "product subcategory" matches
    # `ProductSubcategory` *and* `Product`. Base-table selection then picks by
    # graph degree — favouring the shorter, better-connected name and answering a
    # different question with a plausible number. Drop any detected table whose
    # word-tokens are a strict subset of another detected table's — convention
    # agnostic, so it covers snake_case and camelCase alike.
    if len(detected) > 1:
        tokens = {t: set(split_identifier(t)) for t in detected}
        keep = []
        for t in detected:
            subsumed = any(other != t and tokens[t] < tokens[other] for other in detected)
            if not subsumed:
                keep.append(t)
        detected = keep

    # Second pass: attach tables via distinctive (schema-unique) column names
    # mentioned in the query, e.g. "plans" -> subscriptions.plan. Only columns
    # that appear in exactly one table and are not ids/foreign keys qualify, so
    # generic columns cannot pull in the wrong table.
    column_owner: Dict[str, set] = {}
    for table, columns in schema.items():
        for col in columns:
            if is_identifier_name(col):
                continue
            column_owner.setdefault(col, set()).add(table)

    word_forms = set(words)
    for w in list(words):
        word_forms.add(w.rstrip("s"))
        word_forms.add(w + "s")

    for col, owners in column_owner.items():
        if len(owners) != 1:
            continue
        table = next(iter(owners))
        if table in detected:
            continue
        col_forms = {col, col.rstrip("s"), col + "s"}
        if word_forms & col_forms:
            detected.append(table)

    return detected


# Column names, in priority order, that identify a row to a human reader.
_IDENTIFIER_COLUMNS = (
    "name", "full_name", "fullname", "display_name", "username",
    "user_name", "title", "label",
)


def _find_identifier_column(
    table: str,
    schema: Dict[str, List[str]],
    column_roles: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[str]:
    """Return the best human-identifier column for a table, or None.

    Role-driven when ``column_roles`` is available (Phase A): the column the
    semantic layer classified as a person name (then a non-person label) wins,
    regardless of its literal name or position. Falls back to the name heuristic
    (exact match from _IDENTIFIER_COLUMNS, then a ``_name`` suffix) when no role
    is present, so grounding never depends on the enrichment being there.
    """
    from dbbuddy_core.semantic_roles import identifier_columns_for

    role_hits = identifier_columns_for(table, schema, column_roles)
    if role_hits:
        return role_hits[0]

    columns = schema.get(table, [])
    lower = {c.lower(): c for c in columns}
    for candidate in _IDENTIFIER_COLUMNS:
        if candidate in lower:
            return lower[candidate]
    for c in columns:
        if c.lower().endswith("_name"):
            return c
    return None


def ensure_entity_identifier(intent: Dict, query: str, schema: Dict[str, List[str]],
                             column_roles: Optional[Dict[str, Dict[str, str]]] = None) -> None:
    """Prepend a table's identifier column when the entity is named generically.

    "Show all users and their emails" selects only ``email`` because no column is
    literally named "users" — the user's identity is lost. When the query names a
    table entity (e.g. "users") and we've already selected specific columns from
    it, make sure that table's human identifier (e.g. ``name``) is also selected
    so the rows are actually attributable. Mutates ``intent["select"]`` in place.
    """
    select = intent.get("select") or []
    if not select:
        return  # SELECT * fallback handles the column-less case elsewhere.

    query_words = {w.lower().rstrip("s") for w in query.split()}
    selected_tables = {c["table"] for c in select if isinstance(c, dict) and c.get("table")}

    for table in selected_tables:
        # Only act when the query references this table as an entity word.
        if table.rstrip("s") not in query_words:
            continue

        identifier = _find_identifier_column(table, schema, column_roles)
        if not identifier:
            continue

        # Skip if an identifier column for this table is already selected.
        already = any(
            c.get("table") == table and c.get("column", "").lower() in
            {identifier.lower(), *(_IDENTIFIER_COLUMNS)}
            for c in select if isinstance(c, dict)
        )
        if already:
            continue

        # Insert just before the first selected column of this table so the
        # identifier reads first (e.g. name, email).
        insert_at = next(
            (i for i, c in enumerate(select)
             if isinstance(c, dict) and c.get("table") == table),
            len(select),
        )
        select.insert(insert_at, {
            "table": table,
            "column": identifier,
            "alias": identifier,
            "aggregation": None,
        })

    intent["select"] = select


def detect_tables_from_columns(columns: List[Dict]) -> List[str]:
    """Detect which tables are involved based on selected columns.

    Detect multi-table intent from columns.

    Args:
        columns: List of column dicts with table and column keys

    Returns:
        List of unique table names
    """
    # Order-preserving dedup, NOT list(set(...)): the order tables appear in here
    # decides base-table selection and which table an ambiguous measure binds to.
    # A set iterates in hash order, which varies with PYTHONHASHSEED across
    # processes, so the same question produced MAX(products.unit_price) on one run
    # and MAX(order_items.unit_price) on the next — a different answer to an
    # identical query. Retrieval order (by score) is the meaningful order; keep it.
    seen: set = set()
    ordered: List[str] = []
    for col in columns:
        t = col["table"]
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def extract_columns_from_context(retrieved_context: List[Dict], query: str) -> List[Dict]:
    """Extract all relevant columns (not just top-1) from retrieved context.

    FIX: Handle list structure correctly and use word-level matching.
    FIX: Build columns in query order inside this function.

    Args:
        retrieved_context: List of matches from vector store
        query: User's natural language query for word matching

    Returns:
        List of column dicts with table, column, and alias (in query order)
    """
    if not isinstance(retrieved_context, list):
        return []

    def normalize(word):
        return word.lower().rstrip("s")

    # Tokenize on word characters so punctuation and possessives don't defeat the
    # match: "age?" must match column "age", and "Alice's" must not become
    # "alice'" (which matched nothing). Splitting on whitespace kept both.
    query_words = [normalize(w) for w in re.findall(r"[A-Za-z0-9_]+", query)]
    query_word_set = set(query_words)
    # Adjacent words re-joined, so a column whose name concatenates them is
    # reachable: "ship mode" → ``l_shipmode``, "market segment" → ``c_mktsegment``.
    # Without this the dimension never enters SELECT, and the grouping pass that
    # reads SELECT emits a grand total for a question that asked for a breakdown.
    query_forms = query_word_set | {normalize(f) for f in expand_query_tokens(query)}

    def column_matches(column: str) -> bool:
        """A retrieved column is a match when the whole name appears as a query
        token, or — for a multi-word column — when every one of its parts does.
        ``hire_date`` never equals any single token of "earliest hire date", so
        whole-name equality alone dropped every snake_case column a user spelled
        out with spaces, and the query failed to ground at all.

        A leading one-to-three character part is dropped before that test: it is
        the table prefix a warehouse schema puts on every column (``l_shipmode``),
        and no user says it. Only the retrieved column is in scope here, not its
        table's other columns, so the prefix is judged by length alone — which is
        why the bar is tighter than ``uniform_column_prefix``'s."""
        norm = normalize(column)
        if norm in query_forms:
            return True
        parts = [normalize(p) for p in split_identifier(column)]
        if len(parts) > 1 and len(parts[0]) <= 3:
            parts = parts[1:]
            if len(parts) == 1:
                return parts[0] in query_forms
        parts = [p for p in parts if len(p) > 1]
        return len(parts) > 1 and all(p in query_forms for p in parts)

    # Step 1: Collect matches (unordered)
    matched = []

    for item in retrieved_context:
        if not isinstance(item, dict):
            continue

        column = item.get("column")
        table = item.get("table")

        if not column or not table:
            continue

        # Use word-level matching instead of score filtering
        if column_matches(column):
            matched.append({
                "table": table,
                "column": column,
                "alias": column,
                "aggregation": None
            })

    # Step 2: Build ordered list (THIS is where ordering happens)
    ordered = []

    for word in query_words:
        for col in matched:
            if col["column"] == word and col not in ordered:
                ordered.append(col)

    # Step 3: Append leftovers
    for col in matched:
        if col not in ordered:
            ordered.append(col)

    # Step 4: RETURN ordered (critical)
    logger.debug("%s %s", "ORDERED:", [c["column"] for c in ordered])
    return ordered


def extract_filters(query: str, schema: Dict[str, List[str]],
                    column_types: Optional[Dict[str, Dict[str, str]]] = None,
                    focus_tables: Optional[List[str]] = None) -> List[Dict]:
    """Extract typed comparison filters, driven entirely by the schema.

    Delegates to the type-handler framework (``dbbuddy_core.type_handlers``):
    with ``column_types`` it produces type-aware comparisons (numeric ``age > 30``,
    date ``created after 2024-01-01``, ``BETWEEN``); without them every column is
    treated as text and only equality is produced (the safe default). No hardcoded
    values or table names — it works on any schema.

    ``focus_tables`` (the planner's resolved measure/column tables) scopes a bare
    temporal phrase like "last month" to the right date column when the text names
    no table.
    """
    from dbbuddy_core.type_handlers import extract_comparisons
    return extract_comparisons(query, schema, column_types, focus_tables=focus_tables)


# Command/filler words and generic entity nouns that are never a filter *value*.
_VALUE_STOPWORDS = frozenset({
    "give", "me", "show", "get", "find", "list", "display", "fetch", "tell",
    "return", "lookup", "look", "search", "all", "the", "a", "an", "of", "for",
    "to", "about", "please", "with", "and", "or", "from", "in", "is", "are", "by",
    "on", "at", "as", "details", "detail", "info", "information", "record",
    "records", "data", "row", "rows", "result", "results", "entry", "entries",
    "named", "called", "name", "where", "who", "whose", "that", "this", "these",
    "those", "everyone", "anybody", "anyone", "their", "his", "her", "its",
    "what", "when", "why", "how", "which", "was", "were", "do", "does", "did",
    "give", "me",
    "user", "users", "customer", "customers", "client", "clients", "employee",
    "employees", "person", "people", "member", "members", "account", "accounts",
})

# Generic entity nouns that can directly precede a name literal ("user Alice").
_ENTITY_NOUNS = frozenset({
    "user", "users", "customer", "customers", "client", "clients", "employee",
    "employees", "person", "people", "member", "members", "account", "accounts",
    "student", "students", "buyer", "buyers", "seller", "sellers", "author",
    "authors", "owner", "owners", "admin", "admins",
})

# Words that mark a query as analytical (aggregation / measure / time); when any
# is present we do NOT guess a bare capitalized token as a value (signal (d)).
_TEMPORAL_WORDS = frozenset({
    "last", "this", "previous", "next", "past", "recent", "today", "yesterday",
    "month", "monthly", "week", "weekly", "year", "yearly", "day", "daily",
    "quarter", "quarterly", "date", "time", "ago", "hour", "minute", "since",
})


def _excluded_value_vocab(schema: Dict[str, List[str]]) -> frozenset:
    """Words that can never be a filter *value* for this schema: command/filler
    stopwords, every schema table/column word, and aggregation/measure/temporal
    terms. Derived from the connected schema, so it adapts to any database."""
    schema_words: set = set()
    for table, cols in schema.items():
        schema_words.add(table.lower())
        schema_words.add(table.lower().rstrip("s"))
        for col in cols:
            schema_words.add(col.lower())
            schema_words.update(col.lower().split("_"))
    return frozenset(
        _VALUE_STOPWORDS | schema_words | _TEMPORAL_WORDS
        | {w.lower() for w in AGGREGATION_KEYWORDS} | set(_MEASURE_FRAGMENTS)
    )


def _drop_reference_valued_filters(filters: List[Dict]) -> List[Dict]:
    """Remove filters whose *value* is a ``table.column`` reference.

    The semantic enhancer appends learned references to the query text, and any
    extractor scanning for "<column> <op> <token>" can pick one up as the literal
    on the right-hand side. The result — ``WHERE encounter.kind =
    'encounter.cost'`` — compares a column against the *name* of another column,
    matches nothing, and presents as an empty result rather than a misread
    question. No legitimate literal has this shape, so dropping is safe.

    Applied centrally rather than inside each extractor: the enhancer feeds all
    of them, so guarding one only moves the symptom.
    """
    kept = []
    for f in filters:
        value = f.get("value")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", value):
            logger.debug("Dropping filter with a column-reference value: %s", f)
            continue
        kept.append(f)
    return kept


def _is_filter_value(token: str, excluded: frozenset) -> bool:
    """A token is a plausible literal value if it's not a stopword/schema/agg
    term, not a bare number, and long enough to be meaningful.

    A dotted token is never a value: ``table.column`` is a *reference*, appended
    by the semantic enhancer as a hint. Treating one as a literal produced
    filters like ``WHERE encounter.kind = 'encounter.cost'`` — a comparison
    against the name of a column, which matches nothing and reads as an empty
    result rather than a misparse.
    """
    t = token.lower()
    if "." in t:
        return False
    # Length-1 tokens are allowed only when alphabetic — a coded dimension value
    # like gender 'F'/'M' is a real filter; a stray digit or symbol is not. Every
    # caller confirms a short candidate against the sampled value index before it
    # becomes a filter, so this cannot invent a one-letter WHERE on its own.
    if t in excluded or t.isdigit():
        return False
    return len(t) >= 2 or t.isalpha()


def extract_value_filters(query: str, schema: Dict[str, List[str]], tables: List[str],
                          column_roles: Optional[Dict[str, Dict[str, str]]] = None,
                          value_index: Optional[Dict[str, list]] = None) -> List[Dict]:
    """Extract literal *value* filters (e.g. a person's name) from a query.

    The legacy ``extract_filters`` only knows a few hardcoded literals (India,
    active, …), so a query like "Give me Alice's details" produced no WHERE and
    returned every row. This maps a detected name literal onto a table's human
    identifier column → ``WHERE users.name = 'Alice'``.

    It is deliberately high-precision (a wrong filter is worse than none): a value
    is taken only from unambiguous signals — a possessive ("Alice's"), an explicit
    marker ("named/called Alice"), an entity noun ("user Alice"), or a lone
    capitalized token in a short, non-analytical lookup ("Get Alice"). Analytical
    queries (aggregations, measures, time ranges) never trigger the last signal.

    Grounding order (Phase C): a candidate that exactly matches a sampled
    dimension value in ``value_index`` binds to *that* column ("Pune" → city),
    because the data says so; only a candidate matching no dimension value falls
    through to the person-name identifier grounding.
    """
    # Exclusion vocabulary derived from the connected schema (no hardcoded values).
    excluded = _excluded_value_vocab(schema)

    candidates: List[str] = []

    def _add(token: str) -> None:
        if _is_filter_value(token, excluded) and token.lower() not in {c.lower() for c in candidates}:
            candidates.append(token)

    # (a) Possessive: "Alice's details".
    for m in re.finditer(r"\b([A-Za-z][\w]*)'s\b", query):
        _add(m.group(1))
    # (b) Explicit marker: "named/called Alice".
    for m in re.finditer(r"\b(?:named|called)\s+([A-Za-z][\w]*)", query, re.IGNORECASE):
        _add(m.group(1))
    # (c) Entity noun then a token: "user Alice", "customer Bob".
    for m in re.finditer(r"\b(\w+)\s+([A-Za-z][\w]*)", query):
        if m.group(1).lower() in _ENTITY_NOUNS:
            _add(m.group(2))
    # (e) Attributive position: a token immediately BEFORE an entity/table noun —
    # "shipped orders", "enterprise customers", "cancelled invoices".
    #
    # Every rule above expects the literal to follow something ("named Alice",
    # "customer Bob"). English puts an adjective in front, so "how many orders
    # with status shipped" filtered while "number of shipped orders" — the same
    # question — did not, and the two answers differed by 5x with no indication
    # that one had silently dropped its filter.
    #
    # Deliberately not a word list: the candidate only survives if the sampled
    # value index below actually holds it as a value of some column. Data decides
    # whether "shipped" is a filter; grammar only decides where to look.
    entity_nouns = {t.lower() for t in schema} | {t.lower().rstrip("s") for t in schema}
    entity_nouns |= _ENTITY_NOUNS
    attributive: set = set()
    for m in re.finditer(r"\b([A-Za-z][\w]*)\s+([A-Za-z][\w]*)\b", query):
        head = m.group(2).lower()
        if head in entity_nouns or head.rstrip("s") in entity_nouns:
            before = len(candidates)
            _add(m.group(1))
            if len(candidates) > before:
                attributive.add(candidates[-1].lower())

    # (f) A token immediately after its own column name: "gender F", "status
    # shipped", "title Senior Engineer". The column word is in the schema, so this
    # is high-precision; the candidate still only survives if the sampled value
    # index below confirms it holds that value. Without this a coded dimension
    # value ('F'/'M', 'd005') never became a candidate at all — no grammar signal
    # fires on "how many employees with gender F", so the filter silently vanished
    # and the count came back as the whole table.
    schema_cols = {c.lower() for cols in schema.values() for c in cols}
    schema_col_parts = {p for c in schema_cols for p in c.split("_") if len(p) > 1}
    col_value_candidates: set = set()
    for m in re.finditer(r"\b([A-Za-z][\w]*)\s+([A-Za-z][\w]*)\b", query):
        head = m.group(1).lower()
        if head in schema_cols or head in schema_col_parts:
            before = len(candidates)
            _add(m.group(2))
            if len(candidates) > before:
                col_value_candidates.add(candidates[-1].lower())

    # (d) A lone capitalized token in a short, non-analytical lookup: "Get Alice".
    words = re.findall(r"[A-Za-z][\w]*", query)
    is_analytical = any(
        w.lower() in excluded and (
            w.lower() in AGGREGATION_KEYWORDS or w.lower() in _TEMPORAL_WORDS
        )
        for w in words
    ) or any(frag in query.lower() for frag in _MEASURE_FRAGMENTS)
    # A capitalized proper-noun token is a plausible name even at position 0
    # ("Alice age", "Alice details"). Sentence openers that would otherwise be
    # caught here (what/who/show/get/give…) are already in the stopword set, so a
    # leading token only survives if it is a genuine non-schema proper noun.
    if not candidates and not is_analytical and len(words) <= 6:
        for w in words:
            if re.fullmatch(r"[A-Z][a-z]+", w):
                _add(w)

    if not candidates:
        return []

    # Phase C: a candidate that exactly matches a sampled dimension value binds to
    # the column that holds it ("Pune" → customers.city). Data beats grammar, so
    # this is resolved before the name grounding and removes the candidate from it.
    from dbbuddy_core.column_values import resolve_value

    filters: List[Dict] = []
    remaining: List[str] = []
    for value in candidates:
        hit = resolve_value(value, value_index, tables)
        if hit:
            t, c = hit
            filters.append({"column": f"{t}.{c}", "operator": "=", "value": value})
        elif value.lower() in attributive or value.lower() in col_value_candidates:
            # A grammar-only guess (adjective before a noun, or a token after a
            # column name), not a name the user typed. If the sampled values do
            # not confirm it, drop it — falling through to name grounding invents
            # a filter like `categories.name = 'shipped'`, which returns zero rows
            # and reads as "there is no such data" rather than "I misread you".
            logger.debug("Discarding unconfirmed grammar candidate %r", value)
        else:
            remaining.append(value)

    if not remaining:
        return filters

    # Ground the remaining literals on a table's human-identifier column. Prefer a
    # table the rest of the pipeline already picked; otherwise the first schema
    # table that has one (so a bare "Get Alice" still resolves to e.g. users.name).
    # The identifier column is chosen by semantic role (person_name) when
    # available, falling back to the name heuristic — so a table with several
    # name-ish columns binds to the right one instead of the first by list order.
    target: Optional[tuple[str, str]] = None
    for table in list(tables or []) + list(schema.keys()):
        identifier = _find_identifier_column(table, schema, column_roles)
        if identifier:
            target = (table, identifier)
            break
    if target is not None:
        table, identifier = target
        # Ground only proper-noun-shaped leftovers ("Alice", "Georgi") as names.
        # A lowercase leftover reached here from a grammar signal (a word after an
        # entity noun, "employees hired") without the value index confirming it —
        # binding it to a name column invents ``first_name = 'hired'`` and answers
        # a different question. Capitalization is the only signal that separates a
        # name the user typed from an ordinary word that happened to follow a noun.
        groundable = [v for v in remaining if v[:1].isupper()]
        filters.extend(
            {"column": f"{table}.{identifier}", "operator": "=", "value": value}
            for value in groundable
        )
    return filters


def infer_table_from_filters(filters: List[Dict], schema: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """Infer the base table from filter columns, driven by the schema.

    Filters emitted here are table-qualified (``users.name``), so the table is
    read straight off the column. For a bare column name we fall back to the
    schema — the table that actually owns that column — instead of any hardcoded
    column→table mapping.
    """
    for f in filters:
        column = f.get("column", "")
        if "." in column:
            return column.split(".", 1)[0]
        if schema:
            for table, cols in schema.items():
                if column.lower() in {c.lower() for c in cols}:
                    return table
    return None


def validate_intent(intent: Dict) -> Dict:
    """Ensure strict structure for intent.

    Args:
        intent: Intent dict to validate

    Returns:
        Validated intent dict with enforced structure
    """
    if not isinstance(intent, dict):
        raise ValueError("Intent must be dict")

    intent.setdefault("tables", [])
    intent.setdefault("columns", [])
    intent.setdefault("filters", [])
    intent.setdefault("aggregation", None)
    intent.setdefault("group_by", None)
    intent.setdefault("order_by", None)
    intent.setdefault("limit", None)
    intent.setdefault("having", None)

    # Enforce types
    if not isinstance(intent["tables"], list):
        intent["tables"] = []

    if not isinstance(intent["columns"], list):
        intent["columns"] = []

    if not isinstance(intent["filters"], list):
        intent["filters"] = []

    return intent


def get_count_target_table(tables: List[str], grouping_table: str) -> str:
    """Determine the correct target table for COUNT aggregation.

    FIX: COUNT should target the "event table", not the grouping table.
    For "users with more than 2 subscriptions":
    - Grouping entity: users
    - Event being counted: subscriptions
    - COUNT should target: subscriptions.id

    Args:
        tables: List of tables in the query
        grouping_table: The table used for grouping

    Returns:
        The target table for COUNT (prefer non-grouping table)
    """
    logger.debug("%s %s", "get_count_target_table TABLES:", tables)
    logger.debug("%s %s", "get_count_target_table GROUPING:", grouping_table)

    # Prefer non-grouping table (event table)
    for table in tables:
        if table != grouping_table:
            logger.debug("%s %s", "get_count_target_table RETURNING:", table)
            return table

    # Fallback to grouping table if only one table
    logger.debug("%s %s", "get_count_target_table FALLBACK TO:", grouping_table)
    return grouping_table


def build_query_intent(query: str, retrieved_context: Dict, vector_store, schema: Dict[str, List[str]],
                       column_types: Optional[Dict[str, Dict[str, str]]] = None,
                       column_roles: Optional[Dict[str, Dict[str, str]]] = None,
                       value_index: Optional[Dict[str, list]] = None,
                       primary_keys: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """Build structured intent from query and retrieved context.

    This function converts retrieved context from vector store into a structured intent object
    that explicitly encodes aggregation, grouping, filtering, and other query patterns.

    Args:
        query: User's natural language query
        retrieved_context: Dict with 'tables' and 'columns' from vector store, or list of matches
        vector_store: VectorStore instance for additional matching if needed
        schema: Database schema for table detection from query words

    Returns:
        Structured intent dict:
        {
            "tables": [],
            "columns": [],
            "aggregation": None,
            "group_by": None,
            "filters": [],
            "limit": None,
            "order_by": None
        }
    """
    # FIX: Remove normalization step - use raw list directly
    # The normalization was preventing extract_columns_from_context from working

    query_lower = query.lower()

    # Initialize intent structure
    intent = {
        "tables": [],
        "columns": [],
        "aggregation": None,
        "group_by": None,
        "filters": [],
        "limit": None,
        "order_by": None,
        "select": [],
        "original_query": query  # FIX: Store original query for fallback detection
    }

    # FIX: Use multi-column extraction with word-level matching
    # Extract all relevant columns from retrieved context
    # Handle both list (raw vector store output) and dict (normalized) formats
    logger.debug("%s %s", "RETRIEVED_CONTEXT TYPE:", type(retrieved_context))
    if isinstance(retrieved_context, list):
        # Raw vector store output - list of matches
        logger.debug("USING RAW LIST EXTRACTION")
        columns = extract_columns_from_context(retrieved_context, query)
    elif isinstance(retrieved_context, dict):
        # Normalized format - dict with "columns" key
        logger.debug("USING DICT EXTRACTION")
        columns = extract_columns_from_context(retrieved_context.get("columns", []), query)
    else:
        logger.debug("UNKNOWN CONTEXT TYPE, USING EMPTY COLUMNS")
        columns = []

    # FIX: Deduplicate columns
    seen = set()
    unique_columns = []
    for col in columns:
        key = (col["table"], col["column"])
        if key not in seen:
            seen.add(key)
            unique_columns.append(col)
    columns = unique_columns

    # FIX: Debug print for final select order
    logger.debug("%s %s", "FINAL SELECT ORDER:", [c["column"] for c in columns])

    # FIX: Map to intent["select"] using the new structure
    intent["select"] = columns

    # Detect tables from both columns and query words
    tables_from_columns = detect_tables_from_columns(columns)
    tables_from_query = detect_tables_from_query(query, schema)

    logger.debug("%s %s", "TABLES FROM COLUMNS:", tables_from_columns)
    logger.debug("%s %s", "TABLES FROM QUERY:", tables_from_query)

    # Retrieval returns the top-k columns by similarity, and in a warehouse the
    # top of that list is the *same measure in every channel*:
    # ``ss_quantity``/``cs_quantity``/``ws_quantity``. Merging their tables in
    # made the planner join three fact tables through whatever dimension linked
    # them, multiplying the answer by millions of rows — for a question that
    # named one of them explicitly. A retrieved table earns its place only by
    # contributing a column the named tables cannot: same tokens, once the table
    # prefix is off, means the same thing.
    if tables_from_query and tables_from_columns:
        named_signatures = set()
        for table in tables_from_query:
            table_columns = schema.get(table, [])
            prefix = uniform_column_prefix(table_columns)
            for col in table_columns:
                named_signatures.add(tuple(column_match_tokens(col, prefix)))

        redundant = []
        for table in tables_from_columns:
            if table in tables_from_query:
                continue
            contributed = [c for c in columns if c.get("table") == table]
            if not contributed:
                continue
            prefix = uniform_column_prefix(schema.get(table, []))
            if all(tuple(column_match_tokens(c["column"], prefix)) in named_signatures
                   for c in contributed):
                redundant.append(table)
        if redundant:
            logger.debug("DROPPED REDUNDANT RETRIEVED TABLES: %s", redundant)
            tables_from_columns = [t for t in tables_from_columns if t not in redundant]
            columns = [c for c in columns if c.get("table") not in redundant]
            intent["select"] = columns

    # Merge both detection sources, order-preserving (columns-by-score first, then
    # query-named). NOT list(set(...)): its hash-order iteration made ambiguous
    # measure/base-table resolution non-deterministic across processes.
    intent["tables"] = list(dict.fromkeys(tables_from_columns + tables_from_query))

    logger.debug("%s %s", "TABLES:", intent["tables"])

    # The words the question used, carried into the plan. A star schema reaches
    # one dimension through several foreign keys ("sold date" vs "ship date"),
    # and only the question says which. Kept *inside* the intent on purpose: the
    # plan cache is keyed on the intent, so two questions that differ only by
    # role must not share a cached plan.
    intent["_query_tokens"] = sorted(expand_query_tokens(query))

    # Whether a grain was *asked for*, independent of whether one resolved. The
    # planner's confidence check reads this to tell a dropped GROUP BY apart from
    # a question that never wanted one.
    intent["_requested_grouping"] = requests_grouping(query)

    # When the query is clearly an aggregation over a measure but names no table
    # (e.g. "total revenue"), seed the table from the measure column so it
    # resolves instead of failing with "No tables detected". Gated on an
    # aggregation/measure signal so genuinely irrelevant queries are still
    # rejected downstream.
    if not intent["tables"]:
        if has_aggregation_signal(query):
            measure = find_numeric_column(schema, [], column_types)
            if measure:
                intent["tables"] = [measure["table"]]
                logger.debug("SEEDED TABLE FROM MEASURE: %s", measure["table"])

    # A record lookup grounded only by a name literal ("Alice's details",
    # "What is Alice's age?") names no table/column the detectors can see. If a
    # value filter resolves ("Alice" → <table>.name), seed that table so the
    # query resolves instead of dying at the "No tables detected" guard below.
    if not intent["tables"]:
        seed_filters = extract_value_filters(query, schema, [], column_roles, value_index)
        if seed_filters:
            seeded = seed_filters[0]["column"].split(".", 1)[0]
            intent["tables"] = [seeded]
            logger.debug("SEEDED TABLE FROM LITERAL: %s", seeded)

    # When the query names a table entity ("users") alongside specific columns,
    # make sure that table's human identifier (e.g. name) is selected too, so
    # "show all users and their emails" returns name + email, not just email.
    ensure_entity_identifier(intent, query, schema, column_roles)

    # Apply fallback select if no columns detected
    if not intent["select"] and intent["tables"]:
        # Import here to avoid circular dependency
        from dbbuddy_core.planner_utils import apply_select_fallback
        logger.debug("FALLBACK SELECT APPLIED")

        # Preserve original tables before fallback to prevent corruption
        original_tables = intent["tables"].copy()

        apply_select_fallback(intent, schema)

        # Do NOT re-detect tables after fallback for "show all" queries
        # The fallback sets select to "*" but should NOT corrupt the tables list
        # Only re-detect if the fallback actually added real columns (not "*")
        if intent["select"] and intent["select"][0].get("column") != "*":
            intent["tables"] = detect_tables_from_columns(intent["select"])
        else:
            # Keep original tables for "show all" queries
            intent["tables"] = original_tables

        logger.debug("%s %s", "TABLES AFTER FALLBACK:", intent["tables"])

    # Final guard: ensure tables is always initialized
    if not intent.get("tables"):
        intent["tables"] = tables_from_query or []
        logger.debug("%s %s", "FINAL GUARD: Restored tables from query:", intent["tables"])

    # Safety check for no tables detected
    if not intent["tables"]:
        raise ValueError("No tables detected from query or columns")

    # FIX: Extract aggregation AFTER fallback select (correct pipeline order)
    # FIX: Skip if aggregation was already set and locked by HAVING logic
    if not intent.get("aggregation") and not intent.get("_aggregation_locked"):
        measure_ties: List[str] = []
        aggregation = aggregation_with_widening(query, intent["select"], schema,
                                                intent["tables"], column_types,
                                                primary_keys, measure_ties)
        # A measure the engine cannot legally sum is not a measure. Dropping it
        # here leaves the question's aggregation signal unsatisfied, which the
        # confidence check reports — better than emitting SQL the database
        # refuses, or (on a permissive engine) a zero that reads as an answer.
        if aggregation and not aggregation_is_type_safe(aggregation, column_types):
            logger.debug("Dropping %s over a non-numeric column: %s",
                         aggregation.get("function"), aggregation.get("column"))
            aggregation = None
        # FIX: Use strict dict structure
        if aggregation:
            intent["aggregation"] = {
            "function": aggregation["function"],
            "column": aggregation["column"]
        }
            # Equally-good measures in other tables: carried so the plan can
            # report the question had more than one reading.
            if measure_ties:
                intent["_measure_ambiguity"] = measure_ties
            logger.debug("%s %s", "AGG STRUCT:", intent["aggregation"])
            # The measure may live in a table the query only referenced
            # implicitly (e.g. "revenue per user" → SUM(orders.total_amount)).
            # Ensure that table is in the list so the planner builds the join,
            # otherwise we'd emit SUM(orders.x) FROM users with no join.
            agg_table = aggregation["column"].get("table")
            if agg_table and agg_table not in intent["tables"]:
                intent["tables"].append(agg_table)
            # PHASE 3 FIX: Wipe select columns - planner builds everything fresh for aggregation
            # BUT: Don't wipe if select has "*" marker (show all query)
            if not (intent["select"] and intent["select"][0].get("column") == "*"):
                # Preserve any dimension the query asked to group *by* ("total
                # salary by gender"): the wipe otherwise dropped ``gender`` from
                # the plan, so the aggregate had no grain and the planner emitted
                # a single grand total for a question that asked for a breakdown.
                grouping_cols = _grouping_columns(query, schema, intent["tables"])
                kept = [c for c in intent["select"]
                        if c.get("column", "").lower() in grouping_cols]
                # A learned mapping reaches this point as ordinary query text
                # (the enhancer appends "payments.amount"), so the measure can
                # masquerade as the dimension and skip the binding below.
                kept = strip_measure_only_select(
                    kept, aggregation, requests_grouping(query))
                # A key and its label both match the dimension's token, and the
                # filter above takes whichever came first. Grouping on the key is
                # the right grain with an unreadable label.
                kept = prefer_label_over_key(kept, schema, column_roles)
                # "by product category" names one dimension, not one per word.
                kept = narrow_to_phrase_head(kept, query)
                # "per <table>" names the dimension as an entity, not a column
                # ("total account balance of customers per nation"). Nothing in
                # the select then survives the wipe, and the planner falls back
                # to grouping by the base table's own key — one row per customer
                # instead of one per nation, which is the right measure at a
                # grain nobody asked for. Bind the dimension table to a column:
                # its human identifier when it has one, else its key.
                agg_column = (intent.get("aggregation") or {}).get("column", {})
                if not kept:
                    # The phrase named a real column that retrieval happened not
                    # to return. Bind it from the schema.
                    for table, col in grouping_column_refs(
                            query, schema, intent.get("tables", [])):
                        if col == agg_column.get("column"):
                            continue
                        # "per customer" matches ``invoices.customer_id`` as
                        # readily as the customers table, and grouping on a
                        # foreign key answers at the right grain with the wrong
                        # label. When the dimension is an entity, let the table
                        # route below pick that entity's own identifier.
                        prefix = uniform_column_prefix(schema.get(table) or [])
                        tail = "".join(column_match_tokens(col, prefix))
                        if is_identifier_name(col) or (prefix and tail.endswith("key")):
                            continue
                        kept = [{"table": table, "column": col, "alias": col,
                                 "aggregation": None}]
                        if table not in intent["tables"]:
                            intent["tables"].append(table)
                        break
                if not kept:
                    dimension_table = extract_grouping_table(
                        query, intent.get("tables", []), schema)
                    agg_table = agg_column.get("table")
                    if dimension_table and dimension_table != agg_table:
                        col = (_find_identifier_column(dimension_table, schema,
                                                       column_roles)
                               or _count_anchor_column(dimension_table, schema))
                        if col:
                            kept = [{"table": dimension_table, "column": col,
                                     "alias": col, "aggregation": None}]
                            if dimension_table not in intent["tables"]:
                                intent["tables"].append(dimension_table)
                # Kept in SELECT so the planner's structural-grouping pass sees the
                # dimension and adds the matching GROUP BY. ``group_by`` intent is
                # left untouched — it carries a group *entity* (a table), a
                # different shape from these column dicts.
                intent["select"] = kept
    else:
        intent["aggregation"] = None

    # PHASE 4: Extract ranking (ORDER BY) and limit intent
    order_by, limit = extract_ranking_and_limit(query)

    # A superlative cannot be both the aggregation and the ordering. When
    # "highest" already became MAX, letting it *also* register as a ranking makes
    # the planner add a GROUP BY and emit 400 rows of per-product maxima — the
    # right function, applied at the wrong grain, for a question that wanted one
    # number. Only an explicit grouping phrase should introduce a grain.
    agg_now = intent.get("aggregation") or {}
    if (agg_now.get("function") in ("MAX", "MIN")
            and order_by
            and not any(f" {marker} " in f" {query_lower} " for marker in ("by", "per"))):
        logger.debug("Superlative consumed by %s; dropping the ranking it also matched",
                     agg_now.get("function"))
        order_by, limit = None, limit

    intent["order_by"] = order_by
    intent["limit"] = limit
    logger.debug("%s %s", "ORDER BY:", order_by)
    logger.debug("%s %s", "LIMIT:", limit)

    # PHASE 4: Extract HAVING clause intent
    having = extract_having(query)
    intent["having"] = having
    logger.debug("%s %s", "HAVING:", having)

    # PHASE 4 FIX: Force COUNT aggregation when HAVING exists
    if having and not intent.get("aggregation"):
        # Infer COUNT aggregation for HAVING queries
        # FIX: COUNT should target the "event table", not the grouping table
        grouping_table = extract_grouping_table(query, intent.get("tables", []), schema)

        # Use helper function to determine correct target table
        target_table = get_count_target_table(intent.get("tables", []), grouping_table)

        # COUNT anchor from the schema, not a hardcoded ``id``: a table with no
        # surrogate key (``departments``, ``dept_emp``) has none, and assuming one
        # produced ``employees.id`` — a column that does not exist — so every
        # HAVING query on such a schema crashed in the planner's column check.
        # Any NOT-NULL column counts rows equally; the table's first column is a
        # safe, schema-driven choice, with a literal ``id`` preferred when present.
        target_cols = schema.get(target_table, [])
        count_col = "id" if "id" in target_cols else (target_cols[0] if target_cols else "id")
        aggregation = {
            "function": "COUNT",
            "column": {
                "table": target_table,
                "column": count_col,
                "alias": count_col,
                "aggregation": None
            }
        }

        intent["aggregation"] = aggregation
        logger.debug(f"HAVING COUNT TARGET: {target_table}.{count_col}")

        # CRITICAL: Lock aggregation to prevent further processing from overriding it
        intent["_aggregation_locked"] = True

    # Also populate columns for backward compatibility
    for col in columns:
        intent["columns"].append({
            "table": col["table"],
            "column": col["column"],
            "confidence": 1.0  # Rule-based match gets high confidence
        })
        if col["table"] not in intent["tables"]:
            intent["tables"].append(col["table"])

    # If no tables from columns, try to extract from context
    if not intent["tables"] and isinstance(retrieved_context, dict):
        intent["tables"] = retrieved_context.get("tables", [])

    # FIX: REMOVED old aggregation extraction that was overwriting correct structure

    # Schema-driven filter extraction: explicit "<column> = <value>" filters plus
    # named-literal lookups ("Alice's details"). Both key off the connected
    # schema — no hardcoded values or table names — so this works on any database.
    # Focus tables = what the planner already resolved (selected columns + the
    # measure/aggregation column's table). A bare temporal phrase ("revenue last
    # month") names no table, so this is how it reaches the measure table's date
    # column instead of being dropped as ambiguous across the whole schema.
    focus_tables = list(intent.get("tables") or [])
    agg_col = (intent.get("aggregation") or {}).get("column") or {}
    if agg_col.get("table") and agg_col["table"] not in focus_tables:
        focus_tables.append(agg_col["table"])
    intent["filters"] = _drop_reference_valued_filters(
        extract_filters(query, schema, column_types, focus_tables=focus_tables))
    # Values already claimed by an explicit column filter (e.g. country = India)
    # must not be re-added as a name literal (name = India).
    claimed_values = {str(f.get("value", "")).lower() for f in intent["filters"]}
    for vf in extract_value_filters(query, schema, intent.get("tables", []), column_roles, value_index):
        if vf not in intent["filters"] and str(vf["value"]).lower() not in claimed_values:
            intent["filters"].append(vf)

    # Reconcile HAVING vs grounded filters. extract_having has no schema context,
    # so "amount greater than 100" claims a HAVING threshold even though the
    # schema-driven filter extractor grounded the same "<op> <value>" text to a
    # real column (WHERE orders.amount > 100). When a column filter owns the
    # comparison, it is not an aggregate threshold — drop the HAVING and any
    # COUNT aggregation that was forced from it.
    having = intent.get("having")
    if having and any(
        f.get("operator") == having.get("operator") and f.get("value") == having.get("value")
        for f in intent["filters"]
    ):
        intent["having"] = None
        if intent.pop("_aggregation_locked", None):
            intent["aggregation"] = None

    # An aggregation inferred only from an injected memory reference (e.g. the
    # semantic enhancer appended "orders.amount", and measure-name heuristics
    # promoted it to SUM) must not survive when that same column is being
    # compared in a WHERE filter and the query has no explicit aggregation
    # wording — "orders with amount greater than 100" asks for rows, not a sum.
    agg = intent.get("aggregation")
    if agg and intent["filters"]:
        has_explicit_agg = (
            any(w in AGGREGATION_KEYWORDS for w in query_lower.split())
            or any(p in query_lower for p in AGGREGATION_PHRASES)
        )
        agg_col = agg.get("column") or {}
        agg_ref = f"{agg_col.get('table')}.{agg_col.get('column')}"
        if not has_explicit_agg and any(f.get("column") == agg_ref for f in intent["filters"]):
            intent["aggregation"] = None

    # Make sure every filtered table is part of the query.
    for f in intent["filters"]:
        col = f.get("column", "")
        if "." in col:
            filt_table = col.split(".", 1)[0]
            if filt_table not in intent["tables"]:
                intent["tables"].append(filt_table)

    # FIX: Force table inference from filters
    table_from_filter = infer_table_from_filters(intent["filters"], schema)
    if table_from_filter:
        intent["tables"] = [table_from_filter] if table_from_filter not in intent["tables"] else intent["tables"]

    # FIX: Remove old extraction code that was overwriting Phase 4 fields
    # Old limit/order_by extraction removed - handled by Phase 4 functions above

    # 1 FIX: Validate intent structure before returning
    intent = validate_intent(intent)

    # FIX: Ensure Phase 4 fields are preserved after validation
    # Validate might rebuild intent, so we need to preserve these
    if "order_by" not in intent:
        intent["order_by"] = order_by
    if "limit" not in intent:
        intent["limit"] = limit
    if "having" not in intent:
        intent["having"] = having

    # FIX: Force single source of truth for select
    intent["select"] = intent.get("select") or intent.get("columns") or []

    # FIX: Debug print for intent select
    logger.debug("%s %s", "INTENT SELECT:", intent.get("select"))

    # FIX: Handle empty select - expected in aggregation mode
    if not intent["select"]:
        logger.debug("SELECT deferred to planner (aggregation mode)")

    # FIX: Debug final aggregation state
    logger.debug("%s %s", "FINAL AGG:", intent.get("aggregation"))

    return intent
