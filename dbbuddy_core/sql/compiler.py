"""SQL compiler: execution plan → SQL string.

Turns an engine-agnostic execution plan (from the query planner) into SQL. Two
modes:

* ``compile_sql(..., parameterize=False)`` — inlined literals, for display and
  validation (never executed).
* ``compile_parameterized_sql(...)`` — ``%s`` placeholders plus ordered bound
  params, for safe execution.

WHERE conditions are delegated to the operator framework (``render_conditions``);
this module owns SELECT / JOIN / GROUP BY / HAVING / ORDER BY / LIMIT assembly.
"""

import re
from typing import Any, Dict, Tuple

from dbbuddy_core.logger import get_logger
from dbbuddy_core.sql.conditions import render_conditions
from dbbuddy_core.sql.exceptions import SQLCompilationError

logger = get_logger()


def ensure_list(value):
    """Ensure value is always a list for safe iteration.

    Args:
        value: Any value (None, list, or single item)

    Returns:
        List: Empty list if None, original list if list, or [value] otherwise
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _is_sqlserver(engine) -> bool:
    """True if *engine* names Microsoft SQL Server (canonical or a shorthand)."""
    return str(engine).lower().strip() in {"sqlserver", "mssql", "sql server", "sql_server"}


# SQL keywords that are legal identifiers in a real customer schema. Not the full
# reserved list of any one engine — the union of the words that actually turn up
# as table/column names, which is what a quoting rule has to cover.
_RESERVED_IDENTIFIERS = frozenset({
    "order", "group", "select", "from", "where", "table", "key", "value", "index",
    "desc", "asc", "when", "case", "check", "column", "primary", "foreign",
    "default", "join", "left", "right", "inner", "outer", "union", "all",
    "distinct", "limit", "offset", "having", "by", "and", "or", "not", "null",
    "as", "in", "is", "like", "between", "exists", "into", "values", "set",
    "user", "range", "rank", "row", "rows", "start", "end", "level", "size",
    "type", "status", "comment", "match", "natural", "using", "cast", "current",
    # Reserved in MySQL / PostgreSQL / T-SQL and realistic as column names.
    # ``to`` is the one that caught this: AirportDB names the arrival airport
    # `flight`.`to` (with `from` for departure — already listed above), so a join
    # on it compiled to `ON flight.to = airport.airport_id` and the parser
    # rejected the statement outright. A reserved-word list is only as good as
    # its coverage, and half a pair is worse than neither.
    "to", "on", "for", "with", "if", "then", "else", "cross", "full", "unique",
    "references", "add", "create", "drop", "delete", "insert", "update", "alter",
    "grant", "revoke", "constraint", "view", "database", "schema", "trigger",
    "procedure", "function", "return", "begin", "commit", "rollback",
    "transaction", "lock", "precision", "position", "both", "leading",
    "trailing", "escape", "collate", "some", "any", "over", "partition",
    "window", "filter", "within", "percent", "except", "intersect",
})

# A plain identifier: ASCII letters/underscore, then letters/digits/underscore.
_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _needs_quoting(name: str, dialect=None) -> bool:
    """Whether an identifier must be quoted to survive the parser.

    Quoting is applied **only when needed** rather than universally. Universal
    quoting would be simpler, but it rewrites every statement the product has
    ever emitted — including the ones users read, copy and paste into their own
    tools — for the benefit of a minority of schemas. Targeted quoting leaves
    ordinary output untouched and fixes the schemas that are currently
    unqueryable.

    Needed when the identifier is a reserved word, contains anything outside
    ``[A-Za-z0-9_]`` (spaces, hyphens, accents, CJK), or starts with a digit.

    ...and, on an engine that folds case, when the identifier is not already in
    that case. A PostgreSQL table created as ``"CUSTOMER_LOG"`` is reported by
    introspection as ``CUSTOMER_LOG``; emitted unquoted, PostgreSQL folds the
    reference to ``customer_log`` and reports that the relation does not exist.
    Nothing was wrong with the name — the mistake is assuming an unquoted
    identifier means what it says on every engine. Found by the first dogfood run
    against a real PostgreSQL target.
    """
    if not name:
        return False
    if not _PLAIN_IDENTIFIER.match(name):
        return True   # space, hyphen, digit-leading, or non-ASCII
    if name.lower() in _RESERVED_IDENTIFIERS:
        return True
    folds = getattr(getattr(dialect, "capabilities", None), "unquoted_identifier_case", None)
    if folds == "lower" and name != name.lower():
        return True
    if folds == "upper" and name != name.upper():
        return True
    return False


def _quote(name: Any, dialect=None) -> str:
    """Quote one identifier for the active dialect, if it needs it."""
    text = str(name)
    if not _needs_quoting(text, dialect):
        return text
    if dialect is not None:
        try:
            return dialect.quote_identifier(text)
        except Exception:  # noqa: BLE001 — fall back to the portable form
            pass
    return '"' + text.replace('"', '""') + '"'


def _quote_ref(text: Any, dialect=None) -> str:
    """Quote a possibly-already-qualified reference, part by part.

    A plan may carry the qualification inside the column string
    (``"competitor_prices.price"``) rather than in a separate ``table`` key.
    Quoting that as one identifier produces `` `competitor_prices.price` `` — a
    single column whose name contains a dot, which no database has. Split first,
    then quote each side.
    """
    s = str(text)
    if s == "*":
        return s
    if "." in s:
        left, _, right = s.partition(".")
        if _PLAIN_IDENTIFIER.match(left) and right:
            return f"{_quote(left, dialect)}.{_quote(right, dialect)}"
    return _quote(s, dialect)


def _ref(table: Any, column: Any, dialect=None) -> str:
    """A qualified column reference with each part quoted as needed."""
    col = _quote_ref(column, dialect)
    return f"{_quote(table, dialect)}.{col}" if table else col


def _quote_condition_columns(specs: list, dialect=None) -> list:
    """Quote the column identifiers of WHERE/HAVING predicate specs.

    The operator framework (``sql/operators.py``) interpolates a condition's
    ``column`` verbatim — it is a plain identifier, not an expression — so a
    reserved-word or non-ASCII qualifier reaches the SQL unquoted unless it is
    quoted here. Without this a schema with a table called ``order`` compiled to
    ``... FROM `order` WHERE order.created_at >= %s`` — the FROM/SELECT were quoted
    but the WHERE was not, which is a syntax error, not a wrong answer. SELECT,
    GROUP BY and HAVING already route through ``_ref``/``_quote_ref``; this closes
    the one clause that did not.

    A separate ``table`` key (bare column) is qualified+quoted; an embedded
    ``table.column`` string is split and quoted part-by-part. Subquery specs
    (EXISTS/ANY/ALL) keep their nested plan untouched — it is compiled recursively
    and quotes itself — while a quantified predicate's own ``column`` is quoted.
    Ordinary identifiers are left byte-identical (``_quote`` only acts when needed).
    """
    out = []
    for spec in specs:
        if not isinstance(spec, dict):
            out.append(spec)
            continue
        new = dict(spec)
        table = new.get("table")
        if new.get("column"):
            new["column"] = (
                _ref(table, new["column"], dialect) if table
                else _quote_ref(new["column"], dialect)
            )
            new.pop("table", None)  # qualifier is now baked into the quoted column
        if new.get("column_ref"):
            new["column_ref"] = _quote_ref(new["column_ref"], dialect)
        out.append(new)
    return out


def _tables_in_scope(base_table: Any, joins: list) -> set:
    """Tables a compiled statement can legally qualify a column with."""
    scope = set()
    if base_table:
        scope.add(str(base_table).lower())
    for join in joins:
        if not isinstance(join, dict):
            continue
        for key in ("table", "right_table", "left_table"):
            value = join.get(key)
            if value:
                scope.add(str(value).lower())
    return scope


def _referenced_tables(*clauses) -> set:
    """Tables named by the ``table`` key of any plan item, across clauses."""
    out = set()
    for clause in clauses:
        if clause is None:
            continue
        items = clause if isinstance(clause, list) else [clause]
        for item in items:
            if not isinstance(item, dict):
                continue
            table = item.get("table")
            if table:
                out.add(str(table).lower())
            # WHERE/HAVING conditions carry the table inside a dotted column.
            out.update(_qualifier_of(item.get("column")))
    return out


def _qualifier_of(column: Any) -> set:
    """The table qualifier of a dotted column reference, if it has a plain one.

    ``"orders.status"`` -> ``{"orders"}``; ``"SUM(products.price)"`` -> ``set()``.
    An expression is not a qualified column — treating the text before its first
    dot as a table name invents references like ``sum(products`` and reports them
    as missing joins, which is a worse error than the one being detected.
    """
    if not isinstance(column, str) or "." not in column:
        return set()
    prefix = column.split(".", 1)[0].strip()
    return {prefix.lower()} if prefix.isidentifier() else set()


def _assert_tables_in_scope(base_table, joins, *clauses) -> None:
    """Refuse to emit SQL that qualifies a column with an unjoined table.

    The planner can pick a column from a table it never worked out how to reach —
    e.g. ``SELECT customers.segment FROM orders``, with no JOIN. The database
    answers that with ``no such column: customers.segment``, which reaches the
    user as an opaque driver error and reaches a developer as a mystery, because
    the SQL *looks* fine until you notice the FROM clause.

    Failing here instead turns an entire class of planner defect into one clear,
    attributable error at the point it is introduced. It is a compiler invariant,
    not a style check: no correct plan can violate it, so there is nothing to
    weigh against catching it.
    """
    scope = _tables_in_scope(base_table, joins)
    if not scope:
        return  # no base table — a different error, raised elsewhere
    referenced = _referenced_tables(*clauses)
    missing = sorted(referenced - scope)
    if missing:
        raise SQLCompilationError(
            f"Execution plan references table(s) {', '.join(missing)} that are not "
            f"in scope (FROM {base_table}"
            + (f" joined to {', '.join(sorted(scope - {str(base_table).lower()}))}"
               if len(scope) > 1 else "")
            + "). The planner selected a column without establishing a join to it."
        )


def _normalize_join_type(join_type: Any) -> str:
    """Turn a plan's join ``type`` into a syntactically complete join keyword.

    The default is the full word ``JOIN``, so the format string that renders a
    join assumed the caller's ``type`` already contained it. A plan saying
    ``{"type": "LEFT"}`` — the obvious thing to write, and what the snapshot
    tests wrote — therefore compiled to ``LEFT customers ON …``: invalid SQL on
    every engine, produced silently, and only discovered when the database
    rejected it. Append ``JOIN`` when the qualifier lacks it.

    A blank/None type falls back to a plain ``JOIN`` rather than emitting a
    leading space or a bare table name.
    """
    text = str(join_type or "").strip().upper()
    if not text:
        return "JOIN"
    if "JOIN" in text.split():
        return text
    return f"{text} JOIN"


def compile_sql(execution_plan: Dict[str, Any], parameterize: bool = False, engine: str = "mysql"):
    """Compile execution plan into SQL with robust handling.

    Args:
        execution_plan: Execution plan from query planner
        parameterize: When True, WHERE/HAVING values are emitted as ``%s``
            placeholders and returned separately as bound params.
        engine: Target SQL engine — controls dialect-specific syntax such as
            row limiting (``LIMIT n`` vs SQL Server ``SELECT TOP n``).

    Returns:
        With parameterize=False (default): the SQL string (values inlined,
        suitable for display and validation).
        With parameterize=True: a ``(sql, params)`` tuple for safe execution.
    """
    logger.debug("COMPILE_SQL CALLED")

    # Ordered values bound to %s placeholders when parameterize=True.
    params: list = []

    # Validate execution plan structure
    if not execution_plan:
        raise ValueError("Execution plan is empty")

    # Phase 7.2: ONE ENTRY NORMALIZATION - enforce dict contract at single boundary
    def enforce_dict_list(lst, name):
        """Force all items in list to be dicts, replacing non-dicts with empty dicts."""
        if not isinstance(lst, list):
            return []
        return [x if isinstance(x, dict) else {} for x in lst]

    # Use ensure_list for safe iteration over list fields
    select = ensure_list(execution_plan.get("select"))
    where = ensure_list(execution_plan.get("where"))
    joins = ensure_list(execution_plan.get("joins"))
    group_by = ensure_list(execution_plan.get("group_by"))
    order_by = execution_plan.get("order_by")
    having = execution_plan.get("having")

    # Phase 7.2: Enforce dict contract on ALL list structures
    select = enforce_dict_list(select, "select")
    where = enforce_dict_list(where, "where")
    joins = enforce_dict_list(joins, "joins")
    group_by = enforce_dict_list(group_by, "group_by")
    if isinstance(order_by, list):
        order_by = enforce_dict_list(order_by, "order_by")
    if isinstance(having, list):
        having = enforce_dict_list(having, "having")

    # Phase 7.2: Guard scalar values - ensure base_table is string, not dict
    base_table = execution_plan.get("base_table", "")
    if isinstance(base_table, dict):
        base_table = base_table.get("table", "")
    elif not isinstance(base_table, str):
        base_table = str(base_table)

    # Phase 7.2: HARD NORMALIZATION - Ensure consistent list contracts
    # Never call .get() on group_by/having/order_by directly - always iterate
    if isinstance(group_by, dict):
        group_by = [group_by]
    elif not isinstance(group_by, list):
        group_by = []

    if isinstance(having, dict):
        having = [having]
    elif not isinstance(having, list):
        having = []

    if isinstance(order_by, dict):
        # Handle aggregation-driven format: {'type': 'aggregation', 'direction': 'DESC'}
        if "column" not in order_by and "type" in order_by:
            # Rebuild from aggregation context
            agg = execution_plan.get("aggregation", {})
            if isinstance(agg, dict):
                col = agg.get("column", {})
                order_by = {
                    "table": col.get("table"),
                    "column": col.get("column"),
                    "aggregation": agg.get("function"),
                    "direction": order_by.get("direction", "DESC")
                }
        order_by = [order_by]
    elif not isinstance(order_by, list):
        order_by = []

    # Phase 7.2: Debug prints to identify rogue strings
    logger.debug("%s %s", "DEBUG SELECT:", select)
    logger.debug("%s %s", "DEBUG WHERE:", where)
    logger.debug("%s %s", "DEBUG JOINS:", joins)
    logger.debug("%s %s", "DEBUG GROUP BY:", group_by)
    logger.debug("%s %s", "DEBUG ORDER BY:", order_by)
    logger.debug("%s %s", "DEBUG HAVING:", having)
    logger.debug("%s %s", "DEBUG BASE TABLE TYPE:", type(base_table))
    logger.debug("%s %s", "DEBUG BASE TABLE VALUE:", base_table)

    # Resolve the dialect so engine-specific operators (e.g. ILIKE) can render
    # correctly. Defensive: an unknown engine (or a missing optional driver) must
    # not break compilation that never needed a dialect — fall back to None, and
    # those operators use their portable form.
    dialect = None
    try:
        from dbbuddy_core.dialects.registry import get_dialect
        dialect = get_dialect(engine)
    except Exception:
        dialect = None

    # Build SELECT clause with proper aliasing and aggregation
    select_parts = []
    for item in select:
        table = item.get("table", "")
        column = item.get("column", "")
        alias = item.get("alias", "")
        aggregation = item.get("aggregation", "")

        # Handle wildcard
        if column == "*" or not column:
            select_parts.append("*")
            continue

        # Build column expression with table qualification. Identifiers are
        # quoted when they need it — a customer table called `order` or a column
        # called `total amount` is otherwise a syntax error, not a wrong answer.
        col_expr = _ref(table, column, dialect) if table else _quote_ref(column, dialect)

        # Apply aggregation
        if aggregation:
            col_expr = f"{aggregation}({col_expr})"

        # Add alias (skip if redundant)
        if alias and alias != column:
            col_expr += f" AS {_quote(alias, dialect)}"

        select_parts.append(col_expr)

    if not select_parts:
        select_parts = ["*"]

    select_clause = ", ".join(select_parts)

    # Build FROM clause with validation
    # Note: base_table is already guarded above at line 92-96
    if not base_table:
        raise ValueError("Execution plan missing base_table")

    from_clause = f"FROM {_quote(base_table, dialect)}"

    # Build JOIN clauses with proper syntax
    # Support new graph-based join format
    join_parts = []
    for join in joins:
        if not isinstance(join, dict):
            logger.warning(f"Invalid join item (not a dict): {join}")
            continue

        # Support both old format (table, on, type) and new format (left_table, right_table, left_key, right_key)
        if "left_table" in join and "right_table" in join:
            # New graph-based format
            left_table = join.get("left_table", "")
            right_table = join.get("right_table", "")
            left_key = join.get("left_key", "")
            right_key = join.get("right_key", "")

            if not left_table or not right_table or not left_key or not right_key:
                logger.warning(f"Invalid join specification: left_table={left_table}, right_table={right_table}, left_key={left_key}, right_key={right_key}")
                continue

            join_parts.append(
                f"JOIN {_quote(right_table, dialect)} ON "
                f"{_ref(left_table, left_key, dialect)} = "
                f"{_ref(right_table, right_key, dialect)}")
        else:
            # Old format for backward compatibility
            table = join.get("table", "")
            on = join.get("on", "")
            join_type = join.get("type", "JOIN")

            if not table or not on:
                logger.warning(f"Invalid join specification: table={table}, on={on}")
                continue

            join_parts.append(f"{_normalize_join_type(join_type)} {_quote(table, dialect)} ON {on}")

    join_clause = " ".join(join_parts)

    # Build WHERE clause — each operator renders itself. Conditions arrive in the
    # planner's dict form ({column, operator, value}); Condition.from_spec keeps
    # that format working, and the operator registry owns the SQL for =, the
    # comparison operators, LIKE, IS [NOT] NULL, BETWEEN, IN / NOT IN, and the
    # dialect-rendered ILIKE. See dbbuddy_core.sql.operators.
    where_parts, where_params = render_conditions(
        _quote_condition_columns(where, dialect), parameterize=parameterize, dialect=dialect)
    params.extend(where_params)

    where_clause = ""
    if where_parts:
        where_clause = "WHERE " + " AND ".join(where_parts)

    # Build GROUP BY clause
    group_by_parts = []
    for group in group_by:
        if not isinstance(group, dict):
            logger.warning(f"Invalid group_by item (not a dict): {group}")
            continue
        table = group.get("table", "")
        column = group.get("column", "")

        if table and column:
            group_by_parts.append(_ref(table, column, dialect))
        elif column:
            group_by_parts.append(_quote_ref(column, dialect))

    group_by_clause = ""
    if group_by_parts:
        group_by_clause = "GROUP BY " + ", ".join(group_by_parts)

    # Build HAVING clause
    having_clause = ""
    if having:
        # HAVING is normalized to a single-element list above; unwrap it back
        # to the dict so the clause is actually emitted.
        if isinstance(having, list):
            having = having[0] if having else None

        if isinstance(having, dict):
            agg_func = having.get("aggregation", "")
            table = having.get("table", "")
            column = having.get("column", "")
            operator = having.get("operator", "")
            value = having.get("value", "")

            if agg_func and table and column and operator and value is not None:
                if parameterize:
                    having_clause = f"HAVING {agg_func}({_ref(table, column, dialect)}) {operator} %s"
                    params.append(value)
                else:
                    having_clause = f"HAVING {agg_func}({_ref(table, column, dialect)}) {operator} {value}"
        else:
            logger.warning(f"HAVING is unexpected type: {type(having)}")

    # Build ORDER BY clause with validation
    order_by_clause = ""

    # Phase 7.2: Guard against order_by being wrong type
    if order_by and not isinstance(order_by, (dict, list)):
        logger.warning(f"ORDER BY is wrong type: {type(order_by)}, skipping")
        order_by = None

    # ``order_by`` was wrapped into a single-element list earlier so that every
    # consumer can iterate safely. Assembly works on one spec — a query has one
    # ordering decision — so unwrap it back here.
    #
    # This unwrap is load-bearing, and its absence was invisible for a long time.
    # The aggregate-aware branches below tested ``isinstance(order_by, dict)``
    # against a value that had *just* been wrapped in a list, so none of them
    # ever ran. Every plan fell through to a path that quoted whatever string it
    # was handed, emitting ``ORDER BY "SUM(payments.amount)"`` — an aggregate
    # expression quoted as if it were a column name. PostgreSQL rejects that
    # outright; SQLite reads a quoted unknown identifier as a *string constant*
    # and returns unsorted rows with no error, which is why a green suite and a
    # broken clause coexisted. See tests/test_order_by_compilation.py.
    ob_spec = None
    if isinstance(order_by, list):
        ob_spec = order_by[0] if order_by else None
    elif isinstance(order_by, dict):
        ob_spec = order_by
    if isinstance(ob_spec, str):
        ob_spec = {"column": ob_spec}   # a bare column name
    if not isinstance(ob_spec, dict):
        ob_spec = None

    if ob_spec:
        # Sanitize direction once, for every branch below.
        direction = str(ob_spec.get("direction", "ASC")).upper()
        if direction not in ("ASC", "DESC"):
            direction = "ASC"

        # FIX: Prioritize aggregation if present (golden rule)
        # ORDER BY must always reuse the same aggregation as SELECT
        if "aggregation" in ob_spec:
            # Structured form (correct) - prioritize this path
            table = ob_spec.get("table", "")
            column = ob_spec.get("column", "")
            agg_func = ob_spec.get("aggregation", "")

            if table and column and agg_func:
                order_by_clause = f"ORDER BY {agg_func}({_ref(table, column, dialect)}) {direction}"
                logger.debug("%s %s", "ORDER BY NEW FORMAT:", order_by_clause)

        elif "(" in str(ob_spec.get("column", "")):
            # Pre-formatted aggregate expression, e.g. "SUM(amount)" or
            # "SUM(products.price)".
            #
            # This is a *string*, so every later correction to the plan — most
            # importantly column repair — passes it by. The plan's
            # ``aggregation`` entry is the same measure in structured form and
            # does get corrected, so it is the authoritative one: prefer it,
            # and fall back to parsing the string only when there is no
            # aggregation to prefer. Without this, repairing the measure fixed
            # the SELECT and left `ORDER BY SUM(products.price)` naming a
            # column that no longer existed.
            col = str(ob_spec.get("column", ""))

            # Allow a qualified inner reference, not just a bare word.
            match = re.match(r"\s*(\w+)\s*\(\s*([\w.]+)\s*\)\s*$", col)
            agg = execution_plan.get("aggregation")
            agg_col = agg.get("column") if isinstance(agg, dict) else None

            if isinstance(agg_col, dict) and agg_col.get("table") and agg_col.get("column"):
                func = (match.group(1) if match else agg.get("function", "")) or agg.get("function", "")
                order_by_clause = (
                    f"ORDER BY {func}({_ref(agg_col['table'], agg_col['column'], dialect)}) {direction}"
                )
            elif match:
                func = match.group(1)
                inner = match.group(2)
                order_by_clause = f"ORDER BY {func}({_quote_ref(inner, dialect)}) {direction}"
            else:
                # An expression this compiler does not recognize. Emitting it
                # verbatim is the only honest option — quoting it would turn a
                # function call into a column name, which is the bug this whole
                # block exists to prevent.
                order_by_clause = f"ORDER BY {col} {direction}"

        else:
            # Simple column without aggregation - check if plan has aggregation to reuse
            column = ob_spec.get("column", "")

            # If the plan aggregates, ORDER BY must name the *aggregate*, not
            # the bare column — otherwise a grouped query orders by a column
            # that is not in the GROUP BY.
            #
            # This used to fire only when the ORDER BY column string matched
            # the aggregation's column exactly. Any step that corrected one
            # and not the other (column repair, aliasing) broke the match and
            # dropped through to the raw string, emitting a stale reference:
            # `SELECT SUM(products.unit_price) … ORDER BY SUM(products.price)`,
            # which the database rejects. When the plan has an aggregation and
            # the ORDER BY is a plain column, the aggregation is what was
            # meant — trust it rather than a string comparison.
            plan_agg = execution_plan.get("aggregation")
            if plan_agg and isinstance(plan_agg, dict):
                plan_col = plan_agg.get("column", {})

                if isinstance(plan_col, dict):
                    plan_table = plan_col.get("table", "")
                    plan_column = plan_col.get("column", "")
                    agg_func = plan_agg.get("function", "")
                    bare = column.split(".")[-1].lower() if column else ""

                    matches = (
                        column == f"{plan_table}.{plan_column}"
                        or bare == str(plan_column).lower()
                        # The ORDER BY names a column that is not a grouping
                        # key, so it can only have meant the measure.
                        or not any(
                            isinstance(g, dict)
                            and str(g.get("column", "")).lower() == bare
                            for g in group_by
                        )
                    )
                    if matches and plan_table and plan_column and agg_func:
                        order_by_clause = f"ORDER BY {agg_func}({_ref(plan_table, plan_column, dialect)}) {direction}"

        # Shared fallback. Reached when no branch above produced a clause — most
        # often a structured spec missing its table. Dropping the ordering
        # silently is worse than ordering by the named column, so emit it,
        # quoted as the identifier it is. An expression is never quoted here;
        # the branches above own that case.
        if not order_by_clause:
            column = ob_spec.get("column", "")
            ob_table = ob_spec.get("table", "")
            if column and "(" not in str(column):
                # Quote the identifier (reserved word / space / non-ASCII) and
                # keep the table qualifier — a bare `ORDER BY group` is both a
                # syntax error and, on a join, ambiguous.
                ref = _ref(ob_table, column, dialect) if ob_table else _quote_ref(column, dialect)
                order_by_clause = f"ORDER BY {ref} {direction}"

    # Build row-limit with validation. MySQL/Postgres use a trailing ``LIMIT n``;
    # SQL Server has no LIMIT, so we emit ``SELECT TOP n`` instead (chosen over
    # OFFSET/FETCH because the latter requires an ORDER BY that may be absent).
    limit_clause = ""
    top_clause = ""
    limit = execution_plan.get("limit")
    if limit is not None:
        try:
            limit_int = int(limit)
            if limit_int > 0:
                if _is_sqlserver(engine):
                    top_clause = f"TOP {limit_int} "
                else:
                    limit_clause = f"LIMIT {limit_int}"
        except (ValueError, TypeError):
            logger.warning(f"Invalid limit value: {limit}")

    # Every table the plan qualified a column with must actually be in scope.
    _assert_tables_in_scope(base_table, joins, select, where, group_by, having, order_by)

    # Assemble SQL with proper spacing
    sql = f"SELECT {top_clause}{select_clause} {from_clause}"
    if join_clause:
        sql += f" {join_clause}"
    if where_clause:
        sql += f" {where_clause}"
    if group_by_clause:
        sql += f" {group_by_clause}"
    # Add HAVING clause after GROUP BY
    if having_clause:
        sql += f" {having_clause}"
    if order_by_clause:
        sql += f" {order_by_clause}"
    if limit_clause:
        sql += f" {limit_clause}"

    if parameterize:
        return sql, params
    return sql


def compile_parameterized_sql(execution_plan: Dict[str, Any], engine: str = "mysql") -> Tuple[str, list]:
    """Compile to a parameterized SQL string plus ordered param values.

    WHERE/HAVING values become ``%s`` placeholders bound through
    ``cursor.execute(sql, params)`` — never interpolated into the string — so
    user-derived values cannot inject SQL. Use this for execution; use
    ``compile_sql`` for a human-readable (inlined) string to display/validate.
    """
    return compile_sql(execution_plan, parameterize=True, engine=engine)
