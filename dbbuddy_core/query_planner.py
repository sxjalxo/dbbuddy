"""Query Planner for converting intent to execution plans.

This module converts structured intent into execution plans, resolving
ambiguities like aggregation targets, join paths, filter grounding, etc.

Explainability Engine - adds reasoning layer alongside SQL.
"""

import re
from typing import Any, Dict, List, Optional

from dbbuddy_core.contracts import fail_contract
from dbbuddy_core.logger import get_logger
from dbbuddy_core.relationship_graph import build_relationship_graph
from dbbuddy_core.planner_utils import (
    determine_base_table,
    find_join_path,
    resolve_joins,
    apply_select_fallback
)

logger = get_logger()

# Planner version — part of the plan cache key, so bumping it retires every
# cached plan at once.
#
# **Bump this in the same commit as any change to planning logic.** Plans are
# cached in Redis, which outlives the process *and* the deployment: without a bump
# a planner fix appears to do nothing for every question that has been asked
# before, and the older and more-used the question, the more likely it is to be
# stale. This is easy to miss precisely because a fresh question proves the fix
# works while the reported one keeps failing.
PLAN_VERSION = "v15"  # v15: grain survives a learned mapping; label over key; head noun of a multi-word dimension


def normalize_column_structure(columns: List[Any], base_table: str = None) -> List[Dict[str, Any]]:
    """Normalize column structures to enforce schema contract.

    Enforce schema at boundaries to prevent pipeline inconsistency.
    Column contract: {"table": str, "column": str, "source": str, "alias": str, "aggregation": str}

    Args:
        columns: List of columns (may be strings or dicts)
        base_table: Default table if column is just a string

    Returns:
        List of normalized column dicts
    """
    normalized = []

    for col in columns:
        if isinstance(col, dict):
            # Already a dict, ensure it has required fields
            normalized.append({
                "table": col.get("table", base_table or ""),
                "column": col.get("column", ""),
                "source": col.get("source", "unknown"),
                "alias": col.get("alias", col.get("column", "")),
                "aggregation": col.get("aggregation", None)
            })
        elif isinstance(col, str):
            # String column, convert to dict with base table
            normalized.append({
                "table": base_table or "",
                "column": col,
                "source": "fallback",
                "alias": col,
                "aggregation": None
            })
        else:
            # Unknown type, skip
            logger.warning(f"Skipping unknown column type: {type(col)}")

    return normalized


def normalize_list_of_dicts(items: List[Any], base_table: str, field_type: str) -> List[Dict[str, Any]]:
    """Normalize list items to enforce dict structure contract.

    Phase 7.2: Global normalization for ALL intent structures.
    Handles order_by, filters, having to prevent string/dict mixing.

    Args:
        items: List of items (may be strings or dicts)
        base_table: Default table for string items
        field_type: Type of field (for logging and structure hints)

    Returns:
        List of normalized dict items
    """
    if not items:
        return []

    normalized = []

    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
        elif isinstance(item, str):
            # Convert string to dict based on field type
            if field_type == "order_by":
                # Parse "column DESC" or just "column"
                parts = item.split()
                column = parts[0]
                direction = parts[1] if len(parts) > 1 else "ASC"
                normalized.append({
                    "table": base_table,
                    "column": column,
                    "direction": direction,
                    "source": "fallback"
                })
            elif field_type in ["filters", "having"]:
                # Parse simple filter strings
                normalized.append({
                    "table": base_table,
                    "column": item,
                    "operator": "=",
                    "value": None,
                    "source": "fallback"
                })
            else:
                # Generic fallback
                normalized.append({
                    "table": base_table,
                    "column": item,
                    "source": "fallback"
                })
        else:
            logger.warning(f"Skipping invalid {field_type} item type: {type(item)}")

    return normalized


def validate_intent(intent: Dict[str, Any]) -> None:
    """Validate intent structure before SQL compilation.

    Pipeline validation layer to fail early on type contract violations.
    This prevents crashes in the compiler by catching issues early.

    Add hard guard to prevent '*' in tables list.

    Args:
        intent: Query intent dict to validate

    Raises:
        AssertionError: If intent structure is invalid
    """
    # Basic structure validation
    assert isinstance(intent, dict), f"Intent must be dict, got {type(intent)}"
    assert "tables" in intent, "Intent must contain 'tables' key"
    assert isinstance(intent["tables"], list), "Intent['tables'] must be a list"

    # Hard guard - NEVER allow '*' in tables list
    for table in intent["tables"]:
        assert table != "*", f"Invalid table '*' detected in intent['tables']: {intent['tables']}"
        assert isinstance(table, str), f"Table must be string, got {type(table)}: {table}"

    # Column structure validation
    if "columns" in intent:
        assert isinstance(intent["columns"], list), "Intent['columns'] must be a list"
        for col in intent["columns"]:
            assert isinstance(col, dict), f"Column must be dict, got {type(col)}: {col}"
            assert "table" in col, f"Column must contain 'table' key: {col}"
            assert "column" in col, f"Column must contain 'column' key: {col}"

    # Select structure validation
    if "select" in intent:
        assert isinstance(intent["select"], list), "Intent['select'] must be a list"
        for col in intent["select"]:
            assert isinstance(col, dict), f"Select column must be dict, got {type(col)}: {col}"
            assert "table" in col, f"Select column must contain 'table' key: {col}"
            assert "column" in col, f"Select column must contain 'column' key: {col}"

    # Aggregation structure validation
    if "aggregation" in intent and intent["aggregation"]:
        assert isinstance(intent["aggregation"], dict), "Intent['aggregation'] must be dict if present"
        assert "function" in intent["aggregation"], "Aggregation must contain 'function' key"
        assert "column" in intent["aggregation"], "Aggregation must contain 'column' key"
        assert isinstance(intent["aggregation"]["column"], dict), "Aggregation column must be dict"


def build_explanation(intent: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Build explanation object for query decisions.

    Explainability Engine - structured + UX-friendly format.
    This function runs in the planner (deterministic layer) not intent builder.

    Add confidence indicator to explanations.

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        Explanation dict with summary, interpretation, memory_usage, reasoning, confidence
    """
    explanation = {}

    # 1. Summary (human readable)
    explanation["summary"] = generate_summary(intent, plan)

    # 2. Interpretation (what system understood)
    # Phase 7.2: Ensure aggregation is a dict before .get() calls
    aggregation = plan.get("aggregation", {})
    if not isinstance(aggregation, dict):
        aggregation = {}

    explanation["interpretation"] = {
        "metric": get_metric_explanation(plan),
        "aggregation": aggregation.get("function"),
        "grouping": get_grouping_columns(plan)
    }

    # 3. Memory usage (Phase 6 integration)
    explanation["memory_usage"] = extract_memory_usage(intent)

    # 4. Reasoning trace (step-by-step)
    explanation["reasoning"] = build_reasoning_steps(intent, plan)

    # 5. Confidence indicator (Phase 7.2)
    confidence = plan.get("confidence", 0.0)
    explanation["confidence"] = {
        "score": confidence,
        "level": "high" if confidence >= 0.8 else "medium" if confidence >= 0.5 else "low",
        "breakdown": calculate_confidence_breakdown(intent, plan)
    }

    # 6. Inferred assumptions (Phase 7.2)
    explanation["assumptions"] = extract_assumptions(intent, plan)

    return explanation


def extract_assumptions(intent: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
    """Extract inferred assumptions made during query planning.

    Highlight inferred assumptions to make the system more transparent.
    Examples:
    - "Assumption: 'top users' interpreted as highest total payments"
    - "Assumption: 'per' keyword triggered COUNT aggregation"

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        List of assumption strings
    """
    assumptions = []
    original_query = intent.get("original_query", "").lower()

    # Detect default metric inference for "top" queries
    if any(word in original_query for word in ["top", "highest", "most", "best"]):
        agg = plan.get("aggregation")
        if agg:
            func = agg["function"]
            col = f"{agg['column']['table']}.{agg['column']['column']}"
            if func == "SUM" and "amount" in col:
                assumptions.append(f"Assumption: 'top' query interpreted as highest {col}")
            elif func == "COUNT":
                assumptions.append("Assumption: 'top' query interpreted as most frequent items")
            else:
                assumptions.append(f"Assumption: 'top' query interpreted by {func}({col})")

    # Detect COUNT inference from "per" keyword
    if "per" in original_query:
        agg = plan.get("aggregation")
        if agg and agg["function"] == "COUNT":
            assumptions.append("Assumption: 'per' keyword triggered COUNT aggregation for grouping")

    # Detect grouping inference from "per" pattern
    if "per" in original_query and plan.get("group_by"):
        import re
        per_match = re.search(r"per\s+(\w+)", original_query)
        if per_match:
            entity = per_match.group(1)
            group_cols = get_grouping_columns(plan)
            if group_cols:
                assumptions.append(f"Assumption: 'per {entity}' inferred grouping by {group_cols[0]}")

    # Detect default column selection for vague queries
    if len(intent.get("columns", [])) == 0 and len(intent.get("tables", [])) > 0:
        vague_patterns = ["data", "show", "list", "get", "information"]
        if any(pattern in original_query for pattern in vague_patterns):
            assumptions.append(f"Assumption: Selected all available columns from {intent['tables'][0]}")

    # Detect inferred dimension for ranking without explicit grouping
    if any(word in original_query for word in ["top", "highest", "most"]) and plan.get("group_by"):
        group_cols = get_grouping_columns(plan)
        if group_cols and "name" in group_cols[0]:
            assumptions.append("Assumption: Inferred 'name' column for grouping in ranking query")

    return assumptions


# How much confidence one dropped clause costs.
#
# Sized to be decisive rather than cosmetic. Levels are high >= 0.8,
# medium >= 0.5, and a typical base score is 0.85, so a single drop has to land
# the answer in "low": a query that silently answers a different question is not
# a middling answer. It is still a taper, not a cut-off — the plan is returned,
# with the reason attached, rather than suppressed.
DROPPED_CLAUSE_PENALTY = 0.4

# (label, key in the intent, key in the plan). Order fixes the reported order.
_CLAUSE_CHECKS = (
    ("aggregation", "aggregation", "aggregation"),
    ("grouping", "group_by", "group_by"),
    ("filter", "filters", "where"),
    ("ordering", "order_by", "order_by"),
)


def detect_dropped_clauses(intent: Dict[str, Any], plan: Dict[str, Any]) -> list:
    """Clauses the question asked for that the compiled plan does not contain.

    Confidence scoring was bonus-only: it rewarded structure the planner
    *found* and had no term at all for structure it had been asked for and
    failed to produce. So a plan that lost its GROUP BY, or its filter, or its
    aggregation, still reported high confidence — the engine was most reassuring
    exactly when it had stopped answering the question.

    This compares intent against plan and nothing else, so it stays
    schema-agnostic: it never needs to know what a table or a column is called.

    Presence only, deliberately. Grouping by the *wrong* column is a different
    defect with its own signal (ambiguity detection); folding it in here would
    make one number mean two things and make both harder to trust.
    """
    if not isinstance(intent, dict) or not isinstance(plan, dict):
        return []
    dropped = [
        label
        for label, intent_key, plan_key in _CLAUSE_CHECKS
        if intent.get(intent_key) and not plan.get(plan_key)
    ]

    # Grouping is not carried in ``intent["group_by"]`` — that key holds a group
    # *entity* (a table). The dimension itself ends up as a plain column in
    # ``intent["select"]``, which cannot be used as the signal either: the select
    # list also carries retrieval residue, so "how many customers are there"
    # arrives with ``customers.name`` in it and would look like a request to
    # group by name.
    #
    # So the intent builder records the request explicitly when it sees a
    # grouping phrase (see intent_builder.requests_grouping). That is the only
    # place that can know, and keeping the phrase detection there means natural
    # language is parsed in one place rather than two.
    if "grouping" not in dropped and intent.get("_requested_grouping") and not plan.get("group_by"):
        dropped.insert(1 if "aggregation" in dropped else 0, "grouping")

    return dropped


def calculate_confidence_breakdown(intent: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate confidence breakdown for visualization.

    Confidence visualization with progress bar and breakdown.
    Shows what contributes to confidence score and what reduces it.

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        Dict with confidence breakdown components
    """
    breakdown = {
        "base_score": 0.0,
        "ambiguity_penalty": 0.0,
        "fallback_penalty": 0.0,
        "dropped_clause_penalty": 0.0,
        "schema_coverage_bonus": 0.0,
        "final_score": plan.get("confidence", 0.0),
        "factors": []
    }

    # Base confidence from component resolution
    breakdown["base_score"] = 0.85  # Typical base confidence

    # Ambiguity penalty
    ambiguities = plan.get("ambiguities", [])
    breakdown["ambiguity_penalty"] = len(ambiguities) * 0.1
    if ambiguities:
        breakdown["factors"].append({
            "type": "penalty",
            "name": "Ambiguity",
            "value": -breakdown["ambiguity_penalty"],
            "description": f"{len(ambiguities)} ambiguity(ies) detected"
        })

    # Fallback penalty
    fallback_penalty = 0.0
    if plan.get("base_table") and not intent.get("tables"):
        fallback_penalty += 0.15
        breakdown["factors"].append({
            "type": "penalty",
            "name": "Inferred Table",
            "value": -0.15,
            "description": "Base table was inferred from query"
        })
    if not intent.get("columns") and plan.get("select"):
        fallback_penalty += 0.1
        breakdown["factors"].append({
            "type": "penalty",
            "name": "Inferred Columns",
            "value": -0.1,
            "description": "Columns were inferred from schema"
        })
    breakdown["fallback_penalty"] = fallback_penalty

    # Schema coverage bonus
    selected_tables = set(col["table"] for col in plan.get("select", []))
    intent_tables = set(intent.get("tables", []))
    if intent_tables and selected_tables == intent_tables:
        breakdown["schema_coverage_bonus"] = 0.05
        breakdown["factors"].append({
            "type": "bonus",
            "name": "Schema Coverage",
            "value": 0.05,
            "description": "All intent tables covered in query"
        })

    # Dropped clauses — the one factor that can move this from "confident" to
    # "not confident", and the reason the breakdown is no longer bonus-only.
    dropped = detect_dropped_clauses(intent, plan)
    if dropped:
        breakdown["dropped_clause_penalty"] = len(dropped) * DROPPED_CLAUSE_PENALTY
        breakdown["factors"].append({
            "type": "penalty",
            "name": "Dropped Clauses",
            "value": -breakdown["dropped_clause_penalty"],
            "description": (
                "The question asked for " + ", ".join(dropped)
                + " that the query does not contain"
            ),
        })

    # Add positive factors
    if plan.get("aggregation"):
        breakdown["factors"].append({
            "type": "bonus",
            "name": "Aggregation Detected",
            "value": 0.1,
            "description": f"{plan['aggregation']['function']} aggregation identified"
        })

    if plan.get("group_by"):
        breakdown["factors"].append({
            "type": "bonus",
            "name": "Grouping Detected",
            "value": 0.05,
            "description": "Grouping structure identified"
        })

    if len(intent.get("tables", [])) > 1:
        breakdown["factors"].append({
            "type": "bonus",
            "name": "Multi-table Join",
            "value": 0.05,
            "description": "Successfully joined multiple tables"
        })

    return breakdown


def generate_failure_transparency(intent: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Generate failure transparency information when execution is blocked.

    Failure transparency - show what was unclear, assumed, needed.
    When execution is blocked due to low confidence, provide detailed transparency.

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        Dict with failure transparency information
    """
    transparency = {
        "what_was_unclear": [],
        "what_was_assumed": [],
        "what_is_needed": []
    }

    # Phase 7.2: Ensure plan is a dict before .get() calls
    if not isinstance(plan, dict):
        plan = {}

    # What was unclear
    ambiguities = plan.get("ambiguities", [])
    if not isinstance(ambiguities, list):
        ambiguities = []

    for ambiguity in ambiguities:
        transparency["what_was_unclear"].append(ambiguity)

    # What was assumed (from extract_assumptions)
    assumptions = extract_assumptions(intent, plan)
    transparency["what_was_assumed"] = assumptions

    # What is needed to resolve
    if "Ambiguous 'per' query" in str(ambiguities):
        transparency["what_is_needed"].append("Specify whether you want count, distribution, or sum for the 'per' query")
    if "Ambiguous ranking" in str(ambiguities):
        transparency["what_is_needed"].append("Specify which metric to use for ranking (revenue, orders, count, etc.)")
    if "Query is vague" in str(ambiguities):
        transparency["what_is_needed"].append("Specify which columns you want to retrieve")
    if "Multiple aggregation targets" in str(ambiguities):
        transparency["what_is_needed"].append("Specify which column to aggregate")
    if "Multiple join paths" in str(ambiguities):
        transparency["what_is_needed"].append("Specify which tables to include in the join")

    # If no specific needs identified, provide general guidance
    if not transparency["what_is_needed"]:
        transparency["what_is_needed"].append("Try rephrasing your query with more specific terms")
        transparency["what_is_needed"].append("Use exact table or column names from your schema")

    return transparency


def generate_summary(intent: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """Generate human-readable summary of query execution.

    Enhanced with specific data for smarter UX.

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        Human-readable summary string
    """
    agg = plan.get("aggregation")
    grouping = plan.get("group_by", [])

    if agg:
        metric = f"{agg['column']['table']}.{agg['column']['column']}"
        func = agg["function"]

        if grouping:
            visible_grouping = [g for g in grouping if not g.get("is_hidden")]
            group_item = visible_grouping[0] if visible_grouping else grouping[0]
            group_col = f"{group_item['table']}.{group_item['column']}"
            return f"Calculated {func} of {metric} grouped by {group_col}"

        return f"Calculated {func} of {metric}"

    # Fallback: more specific based on what was selected
    tables = intent.get("tables", [])
    columns = intent.get("columns", [])

    if columns and len(columns) == 1:
        col = columns[0]
        return f"Retrieved {col['table']}.{col['column']}"

    if tables:
        return f"Selected data from {tables[0]}"

    return "Retrieved relevant data based on your query"


def get_metric_explanation(plan: Dict[str, Any]) -> Optional[str]:
    """Extract metric explanation from execution plan.

    Args:
        plan: Execution plan from planner

    Returns:
        Metric string (e.g., "payments.amount") or None
    """
    agg = plan.get("aggregation")
    if not agg:
        return None

    table = agg["column"]["table"]
    column = agg["column"]["column"]

    return f"{table}.{column}"


def get_grouping_columns(plan: Dict[str, Any]) -> List[str]:
    """Extract visible grouping columns from execution plan.

    Always return table.column format for consistency.

    Args:
        plan: Execution plan from planner

    Returns:
        List of visible grouping column names in table.column format
    """
    # Accept either the full plan dict or a raw group_by list (defensive:
    # callers have historically passed both forms).
    if isinstance(plan, list):
        group_by = plan
    elif isinstance(plan, dict):
        group_by = plan.get("group_by", [])
    else:
        return []

    visible = [
        f"{g['table']}.{g['column']}"
        for g in group_by
        if isinstance(g, dict) and not g.get("is_hidden")
    ]

    return visible


def extract_memory_usage(intent: Dict[str, Any], scope: str = None) -> List[Dict[str, str]]:
    """Extract memory usage from intent for explainability.

    Show which terms were mapped using learned memory.
    Dynamic version that loads actual semantic memory.
    Filter out direct table/column matches to reduce noise.

    Args:
        intent: Structured intent from intent_builder

    Returns:
        List of memory usage entries with term, mapped_to, source, and used flag
    """
    # Explainability must show what *this* database's memory contributed. Reading
    # the global store would report mappings that never applied to the query.
    from dbbuddy_core.learning_engine import DEFAULT_SCOPE, load_memory

    memory = load_memory(scope or intent.get("memory_scope") or DEFAULT_SCOPE)
    query_words = intent.get("original_query", "").lower().split()
    tables = intent.get("tables", [])

    usage = []

    for word in query_words:
        if word in memory.get("mappings", {}):
            # Get the best mapping for this term
            best_mapping = max(
                memory["mappings"][word],
                key=memory["mappings"][word].get
            )

            # Filter out direct table/column matches (noise)
            # Only show semantic transformations, not direct matches
            if word in tables:
                # Skip if the term is a direct table name match
                continue

            usage.append({
                "term": word,
                "mapped_to": best_mapping,
                "source": "learned",
                "used": True  # All extracted mappings are considered used
            })

    return usage


def build_reasoning_steps(intent: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
    """Build step-by-step reasoning trace for explainability.

    Decision-based reasoning with cognitive flow.
    Order: interpret → compute → structure → execute

    Upgrade to actual reasoning instead of pipeline narration.
    Show WHY decisions were made, not just WHAT was done.
    Example: "Used 'per user' to infer grouping by users.name" instead of "Applied grouping"

    Args:
        intent: Structured intent from intent_builder
        plan: Execution plan from planner

    Returns:
        List of reasoning step strings
    """
    steps = []
    original_query = intent.get("original_query", "").lower()

    # 1. Interpretation (memory mapping with context)
    memory_usage = extract_memory_usage(intent)
    if memory_usage:
        mapped_terms = [m["term"] for m in memory_usage]
        steps.append(f"Interpreted '{', '.join(mapped_terms)}' using learned semantic memory")

    # 2. Computation (aggregation with reasoning)
    if plan.get("aggregation"):
        agg = plan["aggregation"]
        func = agg["function"]
        col = f"{agg['column']['table']}.{agg['column']['column']}"

        # Explain WHY aggregation was chosen
        if "sum" in original_query or "total" in original_query or "revenue" in original_query:
            steps.append(f"Used 'sum/total' intent to calculate {func}({col})")
        elif "average" in original_query or "avg" in original_query:
            steps.append(f"Used 'average' intent to calculate {func}({col})")
        elif "count" in original_query:
            steps.append(f"Used 'count' intent to calculate {func}({col})")
        elif "per" in original_query and func == "COUNT":
            steps.append("Used 'per' keyword to infer COUNT aggregation for grouping")
        elif any(word in original_query for word in ["top", "highest", "most"]):
            steps.append(f"Inferred {func}({col}) as default metric for ranking query")
        else:
            steps.append(f"Calculated {func}({col}) based on query context")

    # 3. Structure (grouping with reasoning)
    if plan.get("group_by"):
        group_cols = get_grouping_columns(plan)
        if group_cols:
            group_str = ", ".join(group_cols)

            # Explain WHY grouping was chosen
            if "per" in original_query:
                # Extract what comes after "per"
                import re
                per_match = re.search(r"per\s+(\w+)", original_query)
                if per_match:
                    entity = per_match.group(1)
                    steps.append(f"Used 'per {entity}' to infer grouping by {group_str}")
                else:
                    steps.append(f"Used 'per' pattern to infer grouping by {group_str}")
            elif plan.get("aggregation"):
                steps.append(f"Added grouping by {group_str} to aggregate results correctly")
            else:
                steps.append(f"Grouped by {group_str} based on query structure")

    # 4. Execution (joins with reasoning)
    if len(intent.get("tables", [])) > 1:
        tables = intent["tables"]
        steps.append(f"Joined {', '.join(tables)} to relate data across tables")

    # 5. Ranking (ordering with reasoning)
    if plan.get("order_by"):
        order = plan["order_by"]
        direction = order.get("direction", "ASC")
        if "column" in order:
            col = f"{order['table']}.{order['column']}" if "table" in order else order["column"]
            if direction == "DESC":
                steps.append(f"Ordered by {col} descending to show highest values first")
            else:
                steps.append(f"Ordered by {col} ascending")

    # 6. Limiting (with reasoning)
    if plan.get("limit"):
        limit = plan["limit"]
        if "top" in original_query:
            steps.append(f"Limited to {limit} results to show top items")
        else:
            steps.append(f"Limited to {limit} results for performance")

    return steps


def has_grouping_signal(query: str) -> bool:
    """Check if query contains grouping keywords.

    Phase 6.2: Detect grouping intent from query keywords.

    Args:
        query: Original user query

    Returns:
        True if grouping keywords detected, False otherwise
    """
    keywords = ["per", "by", "each"]
    return any(k in query.lower() for k in keywords)


def is_dimension_column(col: Dict[str, Any]) -> bool:
    """Check if a column is a dimension (can be used for GROUP BY).

    Phase 6.2: Filter out measure columns and raw IDs from GROUP BY.

    Args:
        col: Column dict with 'column' key

    Returns:
        True if column is a dimension, False if measure or raw ID
    """
    name = col["column"]

    # NEVER group by measures (things you aggregate)
    if name in ["amount", "price", "value", "total", "count", "sum", "avg"]:
        return False

    # NEVER group by raw IDs (except hidden safety)
    if name == "id":
        return False

    return True


def split_columns(intent: Dict[str, Any]) -> tuple:
    """Split columns into dimensions and metrics based on aggregation.

     Separate dimensions vs metrics for GROUP BY logic.
     FIX: Added safety guard for aggregation structure.
     FIX: Proper dimension/metric separation logic.

    Args:
        intent: Query intent with aggregation info

    Returns:
        Tuple of (dimensions, metrics) lists
    """
    agg = intent.get("aggregation")

    #  FIX: Safety guard - ensure aggregation is dict format
    if not agg or not isinstance(agg, dict):
        return intent.get("select", []), []

    agg_col = agg["column"]

    dimensions = []
    metrics = []

    for col in intent.get("select", []):
        # FIX: Compare column objects, not just column names
        if col["table"] == agg_col["table"] and col["column"] == agg_col["column"]:
            metrics.append(col)
        else:
            dimensions.append(col)

    return dimensions, metrics


def _tables_referenced_by_plan(execution_plan: Dict[str, Any],
                               named_tables: List[str]) -> List[str]:
    """Every table the plan touches: the ones the intent named, plus the ones its
    clauses resolved to while planning.

    Order is preserved and duplicates dropped, because join resolution walks the
    list and the first entries decide the shape of the path.
    """
    ordered: List[str] = []
    seen = set()

    def add(name):
        if not name or not isinstance(name, str):
            return
        key = name.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(name)

    for table in named_tables or []:
        add(table)

    for key in ("select", "columns", "where", "group_by", "order_by", "having"):
        clause = execution_plan.get(key)
        items = clause if isinstance(clause, list) else ([clause] if clause else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            add(item.get("table"))
            column = item.get("column")
            if isinstance(column, str) and "." in column:
                prefix = column.split(".", 1)[0].strip()
                if prefix.isidentifier():
                    add(prefix)
    return ordered


class PlanValidationError(ValueError):
    """A plan is internally inconsistent with the schema it was built against.

    Raised instead of letting the statement reach the database, where the same
    defect surfaces as a driver message (``no such column: products.price``) that
    names the symptom, reaches the user as an opaque 500, and gives a developer no
    indication which planning step invented the reference.
    """


# A pre-formatted aggregate expression as it appears in a plan's ORDER BY:
# "SUM(amount)" or "SUM(products.unit_price)". Anchored so a bare column never
# matches, and the inner reference may be qualified.
_AGG_EXPR_RE = re.compile(r"\s*(\w+)\s*\(\s*([\w.]+)\s*\)\s*$")


def _repair_column(name: str, columns: List[str]) -> Optional[str]:
    """The column ``name`` most likely meant, or None if it is not decidable.

    Only *unambiguous* repairs are made. A phrase like "unit price" reaches here
    as ``price`` while the schema calls it ``unit_price``; that is a real column
    the user named in real words, and refusing it would be pedantry. But
    ``name`` against a table holding both ``first_name`` and ``last_name`` has no
    right answer, and guessing one silently would be worse than failing — so
    ambiguity returns None and the caller raises.

    Order: exact (case-insensitive), separator-insensitive, then a unique
    word-boundary suffix or prefix match.
    """
    if not name:
        return None
    target = name.lower()
    by_lower = {c.lower(): c for c in columns}
    if target in by_lower:
        return by_lower[target]

    def squash(text: str) -> str:
        return text.replace("_", "").replace(" ", "").replace("-", "")

    squashed = {squash(c.lower()): c for c in columns}
    if squash(target) in squashed:
        return squashed[squash(target)]

    # "price" -> "unit_price"; "name" -> "full_name". Word-boundary only, so
    # "id" never matches "paid_on".
    matches = [
        original for lower, original in by_lower.items()
        if lower.endswith("_" + target) or lower.startswith(target + "_")
    ]
    return matches[0] if len(matches) == 1 else None


def _assert_columns_exist(execution_plan: Dict[str, Any], schema: Dict) -> None:
    """Repair near-miss column references; refuse the plan if any cannot be.

    The planner resolves phrases to columns by similarity, and a near-miss
    ("unit price" -> ``price`` when the schema has ``unit_price``) produces SQL
    that is syntactically perfect and references nothing. The database answers
    ``no such column: products.price``, which reaches the user as an opaque error
    and tells a developer nothing about which planning step invented it.

    So: repair what is unambiguous, raise on what is not. Raising is the important
    half — a plan that names a column the schema does not have is wrong, and
    running it anyway only moves the discovery downstream.

    ``*`` and unqualified columns pass through: the former is legal, the latter is
    resolved elsewhere.
    """
    if not schema:
        return
    by_table = {t.lower(): (t, list(cols)) for t, cols in schema.items()}

    missing = []
    # "aggregation" carries the measure as a *nested* {table, column} dict, and the
    # compiler reuses it to render ORDER BY. Repairing only the SELECT left the
    # ORDER BY still naming the phantom column, so the statement failed anyway —
    # every place a column can hide has to be walked, not the obvious ones.
    for key in ("select", "columns", "group_by", "where", "order_by", "having",
                "aggregation"):
        clause = execution_plan.get(key)
        items = clause if isinstance(clause, list) else ([clause] if clause else [])
        expanded = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("column"), dict):
                expanded.append(item["column"])   # aggregation's nested reference
            expanded.append(item)
        for item in expanded:
            if not isinstance(item, dict):
                continue
            table, column = item.get("table"), item.get("column")
            if isinstance(column, dict):
                continue  # handled via the expanded nested entry above
            if not isinstance(column, str) or not column or column == "*":
                continue
            # ORDER BY can arrive as a pre-formatted aggregate expression
            # ("SUM(products.price)") rather than a plain column. It is a string,
            # so every structured correction passes it by — and the compiled plan
            # does not carry the `aggregation` entry, so the compiler cannot fall
            # back to the repaired copy either. Repair the expression in place,
            # here, where the schema is available.
            expr = _AGG_EXPR_RE.match(column)
            if expr:
                inner_table, inner_col = table, expr.group(2)
                if "." in inner_col:
                    prefix, _, rest = inner_col.partition(".")
                    if prefix.isidentifier():
                        inner_table, inner_col = prefix, rest
                entry = by_table.get(str(inner_table).lower())
                if entry:
                    real_table, cols = entry
                    if inner_col.lower() not in {c.lower() for c in cols}:
                        repaired = _repair_column(inner_col, cols)
                        if repaired is None:
                            missing.append(f"{inner_table}.{inner_col}")
                        else:
                            item["column"] = f"{expr.group(1)}({real_table}.{repaired})"
                continue

            qualified = False
            if "." in column:
                prefix, _, rest = column.partition(".")
                if prefix.isidentifier():
                    table, column, qualified = prefix, rest, True
            if not table:
                continue
            entry = by_table.get(str(table).lower())
            if entry is None:
                continue  # unknown table — the join guard reports that instead
            real_table, columns = entry
            if column.lower() in {c.lower() for c in columns}:
                continue

            repaired = _repair_column(column, columns)
            if repaired is None:
                missing.append(f"{table}.{column}")
                continue
            logger.debug("Repaired plan column %s.%s -> %s.%s",
                         table, column, real_table, repaired)
            item["column"] = f"{real_table}.{repaired}" if qualified else repaired

    if missing:
        raise PlanValidationError(
            f"Plan references column(s) that do not exist: {', '.join(sorted(set(missing)))}. "
            "The planner resolved a phrase to a column name the schema does not have."
        )


def _dedupe_joins(joins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse repeated joins, keeping the first occurrence of each target table.

    ``resolve_joins`` walks a path per requested table, so two targets sharing a
    prefix each emit that prefix; ``_reconcile_joins`` then appends anything still
    missing. Neither deduplicates, so a four-hop schema produced
    ``JOIN patient … JOIN patient … JOIN patient …`` — which SQLite rejects with
    ``ambiguous column name: patient.name`` and MySQL rejects as a duplicate
    alias. Joining a table twice without an alias is never intentional.

    Keyed on the joined-to table rather than the whole tuple: reaching the same
    table by two different paths is also a defect, and the first path found is the
    shortest one (the graph search is breadth-first).
    """
    seen_tables = set()
    out: List[Dict[str, Any]] = []
    for join in joins:
        if not isinstance(join, dict):
            continue
        right = str(join.get("right_table") or join.get("table") or "").lower()
        if not right or right in seen_tables:
            continue
        seen_tables.add(right)
        out.append(join)
    return out


def _prune_unused_joins(execution_plan: Dict[str, Any]) -> None:
    """Drop joins to tables no clause references.

    A join is never free when the query aggregates. Joining ``customers`` to
    ``orders`` to ``payments`` and then summing ``customers.credit_limit``
    multiplies each customer's limit by their number of payments — here, 701M
    instead of 94M. The SQL is valid, the number is plausible, and nothing
    surfaces the error except comparing it against a second path.

    Table detection over-collects (a question mentioning "orders" and "payments"
    lists both even when the answer needs neither), so the safe move is to keep a
    join only if some clause actually names its table. Pruning walks from the end:
    a join is load-bearing if it is on the path to a table that *is* referenced.
    """
    joins = _dedupe_joins(execution_plan.get("joins") or [])
    execution_plan["joins"] = joins
    if not joins:
        return

    referenced = {t.lower() for t in _tables_referenced_by_plan(execution_plan, [])}
    base = str(execution_plan.get("base_table") or "").lower()
    referenced.add(base)

    kept: List[Dict[str, Any]] = []
    # Walk in reverse so an intermediate hop is kept when a later hop needs it.
    for join in reversed(joins):
        if not isinstance(join, dict):
            continue
        right = str(join.get("right_table") or join.get("table") or "").lower()
        left = str(join.get("left_table") or "").lower()
        if right in referenced:
            kept.append(join)
            if left:
                referenced.add(left)   # this hop's source is now load-bearing
    kept.reverse()

    if len(kept) != len(joins):
        logger.debug("Pruned %d unused join(s)", len(joins) - len(kept))
    execution_plan["joins"] = kept


def _join_scope(execution_plan: Dict[str, Any]) -> set:
    """Tables a compiled statement could legally qualify a column with."""
    scope = set()
    base = execution_plan.get("base_table")
    if base:
        scope.add(str(base).lower())
    for join in execution_plan.get("joins") or []:
        if isinstance(join, dict):
            for key in ("table", "right_table", "left_table"):
                if join.get(key):
                    scope.add(str(join[key]).lower())
    return scope


def _drop_unreachable_filters(execution_plan: Dict[str, Any]) -> None:
    """Discard filters on tables the finished plan cannot reach.

    Runs after join reconciliation, so a predicate is only dropped once the graph
    has been given every chance to bring its table into scope. What is left is a
    filter the statement cannot express at all — usually a literal that got
    grounded onto an unrelated table — and keeping it produces SQL the compiler
    rejects, which reaches the user as a failure rather than an answer.

    Projected columns are never dropped: losing one changes what the answer *is*,
    not how narrow it is. Dropping a filter widens the result, so it is recorded
    as an ambiguity and the confidence is cut — the caller can see the question
    was answered less specifically than it was asked.
    """
    scope = _join_scope(execution_plan)
    if not scope:
        return

    dropped: List[str] = []

    def out_of_scope(item) -> bool:
        if not isinstance(item, dict):
            return False
        table = item.get("table")
        if not table and isinstance(item.get("column"), str) and "." in item["column"]:
            # Only a bare prefix names a table. An aggregate expression
            # ("SUM(invoices.amount)") also contains a dot, and reading its head
            # as a table dropped every ranked ORDER BY in the suite.
            prefix = item["column"].split(".", 1)[0].strip()
            if prefix.isidentifier():
                table = prefix
        return bool(table) and str(table).lower() not in scope

    for key in ("where", "having", "order_by"):
        clause = execution_plan.get(key)
        if isinstance(clause, list):
            kept = [i for i in clause if not out_of_scope(i)]
            if len(kept) != len(clause):
                dropped.append(key)
                execution_plan[key] = kept
        elif out_of_scope(clause):
            dropped.append(key)
            execution_plan[key] = None

    if not dropped:
        return

    logger.debug("Dropped unreachable %s clause(s) from plan", ", ".join(dropped))
    note = (f"Ignored {'/'.join(dropped)} condition(s) on a table that cannot be "
            f"related to {execution_plan.get('base_table')}")
    execution_plan.setdefault("ambiguities", []).append(note)
    execution_plan["confidence"] = min(execution_plan.get("confidence", 1.0), 0.5)


def _rebase_on_referenced_table(execution_plan: Dict[str, Any], graph: Dict) -> None:
    """Move the base table onto a table the finished plan actually reads.

    The base table is chosen from the intent's table mentions, before the clauses
    are resolved. A question can name one table but resolve every column onto
    another — and when the two are unrelated the graph has no path, so
    reconciliation cannot rescue it and the compiler rejects the plan outright.
    The plan is single-table in that case; it is only anchored to the wrong table.

    Only acts when the base contributes **no** column of its own (so no reference
    is lost) and one referenced table can reach all the others (so the join shape
    is unchanged). Otherwise the plan is left alone for the compiler to judge.
    """
    base = execution_plan.get("base_table")
    referenced = _tables_referenced_by_plan(execution_plan, [])
    if not base or not referenced:
        return
    if any(t.lower() == str(base).lower() for t in referenced):
        return

    # A reachable table is not a wrong base — joining to it answers a different,
    # and usually the intended, question ("how many encounter diagnosis" counts
    # the link table, not the eight distinct diagnoses). Rebase only when the
    # graph offers no path at all, which is the case the compiler would reject.
    if all(find_join_path(graph, base, t) for t in referenced):
        return

    for candidate in referenced:
        others = [t for t in referenced if t.lower() != candidate.lower()]
        if all(t.lower() in graph.get(candidate, {}) or
               find_join_path(graph, candidate, t) for t in others):
            logger.debug("Rebased plan from %s onto %s (base held no columns)",
                         base, candidate)
            execution_plan["base_table"] = candidate
            execution_plan["joins"] = []
            return


def _reconcile_joins(execution_plan: Dict[str, Any], graph: Dict) -> None:
    """Add join paths for tables the finished plan references but never reached.

    Idempotent and additive: an existing join is never rewritten, and a table the
    graph cannot reach is left alone for the compiler to reject.
    """
    base = execution_plan.get("base_table")
    if not base:
        return

    scope = _join_scope(execution_plan)
    referenced = _tables_referenced_by_plan(execution_plan, [])
    missing = [t for t in referenced if t.lower() not in scope]
    if not missing:
        return

    joins = list(execution_plan.get("joins") or [])
    for table in missing:
        for left, right, (left_key, right_key) in find_join_path(graph, base, table):
            if right.lower() in scope:
                continue
            joins.append({"left_table": left, "right_table": right,
                          "left_key": left_key, "right_key": right_key})
            scope.add(right.lower())
    execution_plan["joins"] = joins


def _bound_unlimited_read(execution_plan: Dict[str, Any]) -> None:
    """Give a row-returning plan a row limit if the question did not.

    A plan with no limit asks the database for the whole table. Reading it in
    bounded batches (``query._fetch_bounded``) keeps *this process* alive, but
    the database has still produced every row and the network has still carried
    them. Sending the bound down with the statement is what makes the engine stop
    early: `SELECT * FROM booking` becomes `… LIMIT 1001` and the scan ends after
    a thousand rows instead of 54.3 million.

    One row past the display limit, so the caller can still tell that more rows
    existed without ever holding them. Aggregates are left alone: they return one
    row by construction, and a limit on a grouped query would silently drop
    groups rather than rows.
    """
    if execution_plan.get("limit") is not None:
        return
    if execution_plan.get("aggregation") or execution_plan.get("group_by"):
        return
    if not execution_plan.get("base_table"):
        return
    from dbbuddy_core.execution import MAX_ROWS

    execution_plan["limit"] = MAX_ROWS + 1
    execution_plan["limit_is_guardrail"] = True


def _apply_join_roles(joins: List[Dict], graph: Dict, query_tokens: List[str],
                      schema: Dict) -> None:
    """Re-point each join at the foreign key whose *role* the question named.

    A star schema reaches one dimension through several foreign keys — TPC-DS
    joins ``web_sales`` to ``date_dim`` through both ``ws_sold_date_sk`` and
    ``ws_ship_date_sk``, and to ``customer`` through ``ws_bill_customer_sk`` and
    ``ws_ship_customer_sk``. The graph keeps the first as the default, which
    means "web sales by ship date" was answered with the *sale* date: same
    tables, same shape, same row count, different question.

    Selection is by word overlap between the key's own name and the question,
    scored on the part of the name that is not shared by every candidate — the
    shared part ("date", "customer") cannot discriminate, and the role word
    ("sold", "ship", "bill") is exactly what the user said.
    """
    if not query_tokens or not joins:
        return
    from dbbuddy_core.intent_builder import column_match_tokens, uniform_column_prefix

    tokens = set(query_tokens)
    for join in joins:
        left, right = join.get("left_table"), join.get("right_table")
        edge = (graph.get(left) or {}).get(right)
        alternates = list(getattr(edge, "alternates", []) or [])
        if not alternates:
            continue
        candidates = [(join.get("left_key"), join.get("right_key"))] + alternates
        prefix = uniform_column_prefix(schema.get(left, []) if isinstance(schema, dict) else [])
        # Words every candidate shares carry no signal about which one was meant.
        token_sets = [set(column_match_tokens(from_col, prefix))
                      for from_col, _ in candidates]
        shared = set.intersection(*token_sets) if token_sets else set()
        best, best_score = None, 0
        for (from_col, to_col), owned in zip(candidates, token_sets):
            score = len((owned - shared) & tokens)
            if score > best_score:
                best, best_score = (from_col, to_col), score
        if best and (best[0], best[1]) != (join.get("left_key"), join.get("right_key")):
            logger.debug("Join role: %s.%s selected over %s.%s from the question",
                         left, best[0], left, join.get("left_key"))
            join["left_key"], join["right_key"] = best


def _key_column(table: str, schema: Dict, primary_keys: Dict = None) -> Optional[str]:
    """A column safe to COUNT or GROUP BY as a stand-in for a table's identity.

    Prefers the **declared** primary key (first component of a composite one),
    then a literal ``id``, then ``None`` so the caller can fall back rather than
    invent ``table.id`` on a schema that has no such column — the real
    ``employees`` DB keys ``departments`` on ``dept_no`` and has no ``id``
    anywhere, and assuming one produced ``departments.id`` and a rejected plan.
    """
    pk = (primary_keys or {}).get(table) or []
    cols = schema.get(table, []) if isinstance(schema, dict) else []
    lower = {c.lower(): c for c in cols}
    if pk and pk[0] in cols:
        return pk[0]
    if "id" in lower:
        return lower["id"]
    return None


def plan_query_execution(intent: Dict[str, Any], schema: Dict, vector_store, cache=None, schema_hash: str = None, foreign_keys: Dict = None, relationship_graph: Dict = None, primary_keys: Dict = None, column_roles: Dict = None) -> Dict[str, Any]:
    """Convert structured intent into execution plan.

    This function resolves ambiguities in the intent and produces a concrete
    execution plan that can be directly compiled to SQL.

    Args:
        intent: Structured intent from intent builder
        schema: Database schema
        vector_store: VectorStore instance for column resolution
        cache: Optional Redis cache instance for plan caching
        schema_hash: Optional schema hash for schema-aware caching

    Returns:
        Execution plan dict:
        {
            "base_table": str,
            "joins": [{"table": str, "on": str, "type": str}],
            "select": [{"table": str, "column": str, "alias": str, "aggregation": str}],
            "where": [{"column": str, "operator": str, "value": Any}],
            "group_by": [{"table": str, "column": str}],
            "order_by": {"column": str, "direction": str},
            "limit": int,
            "confidence": float,
            "ambiguities": []
        }
    """
    execution_plan = {
        "base_table": None,
        "joins": [],
        "select": [],
        "where": [],
        "group_by": [],
        "order_by": None,
        "limit": None,
        "confidence": 0.0,
        "ambiguities": []
    }

    # NEW: Plan cache check with schema awareness and versioning
    if cache:
        # Create cache key from intent with planner version
        cache_key = f"{PLAN_VERSION}:{str(intent)}"
        cache_hit = cache.get("plan", cache_key, schema_hash=schema_hash)
        if cache_hit:
            # Validate cache hit structure (cache is optional optimization, never source of truth)
            if not isinstance(cache_hit, dict):
                logger.warning(f"Invalid cached plan type: {type(cache_hit)} - ignoring cache and regenerating")
                # Fall through to generate fresh plan
            else:
                logger.debug("Cache hit: valid execution plan")
                return cache_hit

    # Relationship graph for multi-table reasoning. The context store already
    # built one from the full catalog (declared FKs *and* declared primary keys),
    # so use it when the caller supplies it; rebuilding here from the lightweight
    # schema silently discarded the key metadata and lost joins the context knew
    # about. Rebuilding stays the fallback for callers with no context.
    graph = relationship_graph or build_relationship_graph(schema, foreign_keys)

    # Track confidence scores for plan-level confidence
    confidence_scores = []

    # Apply fallback select if no columns specified
    apply_select_fallback(intent, schema)

    # Normalize column structure to enforce schema contract
    base_table = determine_base_table(intent.get("tables", []), graph)
    if not base_table and intent.get("tables"):
        base_table = intent["tables"][0]

    # Fix base table resolution - never allow "*" as base table
    if base_table == "*":
        base_table = intent.get("tables", [None])[0] if intent.get("tables") else None
        logger.warning(f"Base table was '*', corrected to: {base_table}")

    intent["select"] = normalize_column_structure(intent.get("select", []), base_table)
    intent["columns"] = normalize_column_structure(intent.get("columns", []), base_table)

    # Validate intent structure before proceeding
    try:
        validate_intent(intent)
    except AssertionError as e:
        logger.error(f"Intent validation failed: {e}")
        # Return safe execution plan to prevent crash
        return {
            "base_table": base_table or (intent.get("tables", [None])[0] if intent.get("tables") else None),
            "joins": [],
            "select": intent.get("select", []),
            "where": [],
            "group_by": [],
            "order_by": None,
            "limit": None,
            "confidence": 0.0,
            "ambiguities": [f"Internal validation error: {str(e)}"]
        }

    # Step 1: Determine base table using graph degree (no hardcoding)
    tables = intent.get("tables", [])
    execution_plan["base_table"] = base_table
    if not execution_plan["base_table"] and tables:
        execution_plan["base_table"] = tables[0]  # fallback
    logger.debug("%s %s", "BASE TABLE:", execution_plan["base_table"])
    confidence_scores.append(0.9)  # Base table selection is usually confident

    # Split columns into dimensions vs metrics and handle aggregation
    dimensions, metrics = split_columns(intent)
    aggregation = intent.get("aggregation")

    # FIX: Ensure aggregation exists in plan before ORDER BY logic
    # This prevents planner → explanation mismatch bugs
    if aggregation:
        execution_plan["aggregation"] = aggregation

    # FIX: Add fallback aggregation when ORDER BY exists but aggregation is missing
    order_intent = intent.get("order_by")
    if order_intent and not aggregation:
        # Inject COUNT aggregation for ranking queries without explicit aggregation
        tables = intent.get("tables", [])
        base_table = execution_plan.get("base_table")
        target_table = base_table if base_table else (tables[0] if tables else None)

        anchor = _key_column(target_table, schema, primary_keys) if target_table else None
        if anchor:
            aggregation = {
                "function": "COUNT",
                "column": {
                    "table": target_table,
                    "column": anchor,
                    "alias": anchor,
                    "aggregation": None
                }
            }
            execution_plan["aggregation"] = aggregation
            intent["_aggregation_inferred"] = True
            logger.debug(f"INFERRED AGGREGATION FOR ORDER BY: COUNT({target_table}.{anchor})")

    # FIX: Prevent crash - ensure aggregation has column
    if aggregation and aggregation.get("column"):
        # Build select with aggregation
        agg_func = aggregation["function"]
        agg_col = aggregation["column"]

        # Phase 6.2: Defer GROUP BY decision until after aggregation + HAVING resolution
        # Only build SELECT here, GROUP BY will be decided later
        select = [{
            "table": agg_col["table"],
            "column": agg_col["column"],
            "alias": f"{agg_func.lower()}_{agg_col['column']}",
            "aggregation": agg_func
        }]
        execution_plan["select"] = select
        execution_plan["group_by"] = []
        confidence_scores.append(0.8)
    else:
        # No aggregation - use normal select logic
        # FIX: Intent ALWAYS wins over fallback
        if intent.get("select"):
            execution_plan["select"] = [
                {
                    "table": col["table"],
                    "column": col["column"],
                    "alias": col["column"],
                    "aggregation": None
                }
                for col in intent["select"]
            ]
            confidence_scores.append(0.9)  # High confidence when intent specifies columns
        elif execution_plan["base_table"]:
            # Fallback: select all columns from base table
            execution_plan["select"] = [
                {
                    "table": execution_plan["base_table"],
                    "column": col,
                    "alias": col,
                    "aggregation": None
                }
                for col in schema[execution_plan["base_table"]]
            ]
        else:
            execution_plan["select"] = [{"table": execution_plan["base_table"], "column": "*", "alias": "", "aggregation": None}]

        execution_plan["group_by"] = []

    # Add ORDER BY logic using plan aggregation (not intent)
    order_intent = intent.get("order_by")
    logger.debug("%s %s", "ORDER BY INTENT:", order_intent)

    # FIX: Reference plan aggregation, not intent, for consistency
    agg = execution_plan.get("aggregation")

    if order_intent and agg:
        # FIX: Ensure aggregation is ALWAYS present in order_by
        # Never pass just the column alone - always include aggregation
        execution_plan["order_by"] = {
            "table": agg["column"]["table"],
            "column": agg["column"]["column"],
            "aggregation": agg["function"],  # MUST exist - prevents crashes
            "direction": order_intent["direction"]
        }
        logger.debug("%s %s", "ORDER BY PLAN:", execution_plan["order_by"])
    elif order_intent and not agg:
        # FIX: Fallback for ORDER BY without aggregation (shouldn't happen with inferred aggregation)
        # Add a simple column order by without aggregation
        execution_plan["order_by"] = {
            "column": order_intent.get("column", ""),
            "direction": order_intent.get("direction", "ASC")
        }
        logger.debug("%s %s", "ORDER BY PLAN (NO AGG):", execution_plan["order_by"])

    # Add LIMIT logic
    if intent.get("limit"):
        execution_plan["limit"] = intent["limit"]
        logger.debug("%s %s", " LIMIT PLAN:", execution_plan["limit"])

    # Add HAVING logic using plan aggregation (not intent)
    if intent.get("having") and execution_plan.get("aggregation"):
        agg = execution_plan["aggregation"]

        execution_plan["having"] = {
            "aggregation": agg["function"],
            "table": agg["column"]["table"],
            "column": agg["column"]["column"],
            "operator": intent["having"]["operator"],
            "value": intent["having"]["value"]
        }
        logger.debug("%s %s", " HAVING PLAN:", execution_plan["having"])

    # Step 3: Resolve join paths using graph-based approach.
    #
    # Join resolution must consider every table the plan *ended up* referencing,
    # not only the tables the intent named up front. A question like "orders by
    # customer segment" resolves its grouping column to `customers.segment` during
    # planning, but `intent["tables"]` may still say only `orders` — so nothing
    # ever asked the graph for a path to `customers`, and the plan compiled to
    # `SELECT customers.segment FROM orders`: a column from a table that was never
    # joined. Deriving the set from the clauses closes that gap for every clause at
    # once, rather than patching whichever one surfaced the symptom.
    join_targets = _tables_referenced_by_plan(execution_plan, tables)
    joins = resolve_joins(graph, execution_plan["base_table"], join_targets)
    # Force structure: ensure joins is always a list
    if not isinstance(joins, list):
        logger.warning(f"resolve_joins returned non-list: {type(joins)}")
        joins = []
    _apply_join_roles(joins, graph, intent.get("_query_tokens") or [], schema)
    execution_plan["joins"] = joins
    logger.debug("%s %s", " JOINS:", joins)
    if execution_plan["joins"]:
        confidence_scores.append(0.7)  # Join resolution confidence

    # Phase 6.2: GROUP BY decision AFTER aggregation + HAVING + joins resolution
    # Use execution_plan instead of intent for final grouping decision
    if execution_plan.get("aggregation"):
        # Get final state from execution_plan
        has_aggregation = execution_plan.get("aggregation") is not None
        has_having = execution_plan.get("having") is not None
        has_ranking = execution_plan.get("order_by") is not None
        has_joins = len(execution_plan.get("joins", [])) > 0

        # Check for grouping signals in original query
        grouping_signal_detected = has_grouping_signal(intent.get("original_query", ""))

        # Check for dimension columns
        has_dimension_columns = len([col for col in dimensions if is_dimension_column(col)]) > 0

        # Phase 6.2: Final GROUP BY decision logic
        needs_grouping = (
            grouping_signal_detected     # "per user"
            or has_having                # HAVING requires grouping
            or has_ranking               # ranking requires grouping
            or (has_dimension_columns and has_aggregation)  # dimensions + aggregation
        )

        # Phase 6.2: JOIN + aggregation + ranking ⇒ grouping
        if has_ranking and has_joins and has_aggregation:
            needs_grouping = True

        # Phase 6.2: Ensure ranking queries have dimensions (infer if missing)
        if has_ranking and needs_grouping and not has_dimension_columns:
            base_table = execution_plan.get("base_table")
            if base_table:
                schema_columns = schema.get(base_table, [])
                for col_name in ["name", "title", "description"]:
                    if col_name in schema_columns:
                        dimensions = [{"table": base_table, "column": col_name, "aggregation": None}]
                        has_dimension_columns = True
                        logger.debug(f"INFERRED DIMENSION FOR RANKING: {base_table}.{col_name}")
                        break

        # Build GROUP BY if needed
        group_by = []
        if needs_grouping:
            # Use semantic grouping if available
            if intent.get("group_by"):
                # Tag as semantic grouping (from user intent)
                group_by = [{"table": col["table"], "column": col["column"], "type": "semantic"} for col in intent["group_by"]]
            elif grouping_signal_detected:
                # A grouping phrase ("by gender", "per status") was found. Group by
                # the dimension the user actually named — the columns still in the
                # plan after the metric is split out — before falling back to
                # guessing a name/title/description column. The old code skipped
                # straight to that guess, so "total salary by gender" grouped by
                # nothing (employees has no name/title/description) and collapsed to
                # a single grand total instead of one row per gender.
                dimension_columns = [
                    col for col in dimensions if is_dimension_column(col)
                ]
                if dimension_columns:
                    group_by = [
                        {"table": col["table"], "column": col["column"],
                         "type": "semantic"}
                        for col in dimension_columns
                    ]
                else:
                    base_table = execution_plan.get("base_table")
                    if base_table:
                        schema_columns = schema.get(base_table, [])
                        for col_name in ["name", "title", "description"]:
                            if col_name in schema_columns:
                                group_by = [{"table": base_table, "column": col_name, "type": "semantic"}]
                                break
            else:
                # Structural grouping - use dimension columns
                non_agg_columns = [
                    col for col in dimensions
                    if not col.get("aggregation")
                ]
                dimension_columns = [
                    col for col in non_agg_columns
                    if is_dimension_column(col)
                ]

                group_by = [
                    {
                        "table": col["table"],
                        "column": col["column"],
                        "type": "structural"
                    }
                    for col in dimension_columns
                ]

            # Phase 6.2: Add duplicate safety — include the table's key column in
            # GROUP BY (hidden) so two entities sharing a *display name* are not
            # merged. This applies ONLY when grouping on a name/identity column:
            # for a categorical dimension (``gender``, ``status``) collapsing equal
            # values is the whole point, and adding the key there returned the
            # grain to one row per entity — "total salary by gender" grouped by
            # ``gender, emp_no`` and reported per-employee totals, not per gender.
            # Add the hidden key only when grouping on a *person-name* column —
            # a per-entity display value where two distinct entities can collide
            # and must not be merged. This reads the generic semantic role
            # (``person_name``), not a hardcoded column-name list: a categorical
            # label (``title``) or dimension (``gender``, ``status``) has role
            # ``label``/``dimension`` and is meant to collapse, so it gets no key.
            def _is_identity(table: str, column: str) -> bool:
                role = (column_roles or {}).get(table, {}).get(column)
                return role == "person_name"

            if group_by:
                name_tables = {col["table"] for col in group_by
                               if _is_identity(col["table"], col["column"])}
                for table in name_tables:
                    anchor = _key_column(table, schema, primary_keys)
                    if anchor and not any(
                            col["table"] == table and col["column"] == anchor
                            for col in group_by):
                        group_by.append({
                            "table": table,
                            "column": anchor,
                            "is_hidden": True,
                            "type": "structural"
                        })

            # Phase 6.2: Clean GROUP BY to only include grouping table columns
            if group_by:
                grouping_table = group_by[0]["table"] if group_by else None
                if grouping_table:
                    group_by = [col for col in group_by if col["table"] == grouping_table]

            # Phase 6.2: Rebuild SELECT with GROUP BY columns
            select = []

            # Add group-by columns (respect is_hidden flag)
            for col in group_by:
                if not col.get("is_hidden", False):
                    select.append({
                        "table": col["table"],
                        "column": col["column"],
                        "alias": col["column"],
                        "aggregation": None
                    })

            # Add aggregation column
            agg = execution_plan.get("aggregation")
            if agg:
                select.append({
                    "table": agg["column"]["table"],
                    "column": agg["column"]["column"],
                    "alias": f"{agg['function'].lower()}_{agg['column']['column']}",
                    "aggregation": agg["function"]
                })

            execution_plan["select"] = select
            execution_plan["group_by"] = group_by
            logger.debug("%s %s", "GROUP BY:", group_by)
        else:
            # No grouping needed - keep SELECT as aggregation only
            execution_plan["group_by"] = []
            logger.debug("GROUP BY: []")

        # Phase 6.2: STRICT pure aggregation override (FINAL check after all resolution)
        # This must run AFTER aggregation, HAVING, ranking, joins are all resolved
        # to prevent fallback dimensions from polluting pure aggregation queries
        is_pure_aggregation = (
            execution_plan.get("aggregation") is not None
            and not grouping_signal_detected
            and not has_having
            and not has_ranking
        )

        if is_pure_aggregation:
            # Force global aggregation - no GROUP BY, no dimension columns
            execution_plan["group_by"] = []
            # Rebuild SELECT to only include aggregation column (explicit type field for robustness)
            agg = execution_plan.get("aggregation")
            if agg:
                execution_plan["select"] = [{
                    "type": "aggregation",
                    "table": agg["column"]["table"],
                    "column": agg["column"]["column"],
                    "alias": f"{agg['function'].lower()}_{agg['column']['column']}",
                    "aggregation": agg["function"]
                }]
            logger.debug("PURE AGGREGATION OVERRIDE - GROUP BY: [], SELECT rebuilt to aggregation only")



    # Step 4: Use simple rule-based filters from intent
    # Map intent filters directly to execution_plan where clause
    execution_plan["where"] = [
        {
            "column": f["column"],
            "operator": f["operator"],
            "value": f["value"]
        }
        for f in intent.get("filters", [])
    ]
    confidence_scores.append(0.8)  # Filter binding confidence

    # Step 5: Resolve grouping anchor
    if intent["group_by"]:
        group_by = _resolve_grouping_anchor(
            intent, schema, execution_plan["base_table"], primary_keys
        )
        # Force structure: ensure group_by is always a list
        if not isinstance(group_by, list):
            logger.warning(f"_resolve_grouping_anchor returned non-list: {type(group_by)}")
            group_by = []
        execution_plan["group_by"] = group_by
        confidence_scores.append(0.9)  # Grouping resolution confidence

    # Step 6: Resolve ordering with aggregation context
    if intent["order_by"]:
        order = _resolve_ordering(
            intent, schema, execution_plan["base_table"], execution_plan["select"],
            primary_keys
        )
        # Force structure: ensure order_by is dict or None, never list
        if isinstance(order, list):
            order = order[0] if order else None
        execution_plan["order_by"] = order
        confidence_scores.append(0.8)  # Ordering resolution confidence

    # Step 7: Copy limit
    execution_plan["limit"] = intent["limit"]

    # Phase 7.2: Normalize SELECT structure before compilation
    # Ensure all select items are dicts, never strings
    if execution_plan.get("select"):
        normalized_select = []
        for item in execution_plan["select"]:
            if isinstance(item, dict):
                normalized_select.append(item)
            elif isinstance(item, str):
                # Convert string to dict structure
                if item == "*":
                    normalized_select.append({
                        "table": execution_plan["base_table"],
                        "column": "*",
                        "alias": "",
                        "aggregation": None
                    })
                else:
                    normalized_select.append({
                        "table": execution_plan["base_table"],
                        "column": item,
                        "alias": item,
                        "aggregation": None
                    })
            else:
                logger.warning(f"Skipping invalid select item: {item}")
        execution_plan["select"] = normalized_select

    # Phase 7.2: Normalize ONLY list structures to enforce dict contract
    # WHERE is the ONLY one that should be a list
    execution_plan["where"] = normalize_list_of_dicts(
        execution_plan.get("where"),
        execution_plan["base_table"],
        "filters"
    )

    # Phase 7.2: DO NOT normalize these (they are dict contracts)
    # order_by and having should remain as dicts, not be converted to lists
    if isinstance(execution_plan.get("order_by"), list):
        execution_plan["order_by"] = execution_plan["order_by"][0] if execution_plan["order_by"] else None

    if isinstance(execution_plan.get("having"), list):
        execution_plan["having"] = execution_plan["having"][0] if execution_plan["having"] else None

    # Reconcile joins against the *finished* plan.
    #
    # Joins are resolved early because the GROUP BY and ranking decisions depend
    # on knowing whether the query is multi-table. But `select`, `group_by`,
    # `where` and `order_by` are all populated afterwards, and they are what
    # resolve a phrase like "by customer segment" to `customers.segment`. So the
    # early pass cannot see the very references that need a join, and the plan
    # compiled to `SELECT customers.segment FROM orders` — valid-looking SQL
    # naming a table nothing ever joined.
    #
    # Rather than reorder the pipeline (the GROUP BY decision genuinely needs the
    # early answer), reconcile at the end: anything the plan ended up referencing
    # that is not yet in scope gets a path from the graph, if one exists. When the
    # graph has no path the compiler refuses the plan — a clear error beats SQL
    # the database will reject with "no such column".
    _rebase_on_referenced_table(execution_plan, graph)
    _reconcile_joins(execution_plan, graph)
    _drop_unreachable_filters(execution_plan)
    _assert_columns_exist(execution_plan, schema)
    _prune_unused_joins(execution_plan)

    # Phase 7.2: Strict contract assertions
    # Use .get() — "having" is only set when a HAVING clause exists, so a
    # direct key access would KeyError on every aggregation query without one.
    assert execution_plan.get("having") is None or isinstance(execution_plan.get("having"), dict), \
        f"having must be dict or None, got {type(execution_plan.get('having'))}"
    assert execution_plan.get("order_by") is None or isinstance(execution_plan.get("order_by"), dict), \
        f"order_by must be dict or None, got {type(execution_plan.get('order_by'))}"

    # Step 8: Calculate plan-level confidence with real inputs
    # Enhanced confidence scoring based on actual inputs
    # Instead of just averaging heuristic scores, use:
    # - Semantic match strength (from retrieval)
    # - Ambiguity count (penalty)
    # - Fallback usage (penalty)
    # - Schema coverage (bonus)

    # Base confidence from component scores
    base_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0

    # Apply ambiguity penalty
    ambiguities = _detect_ambiguities(execution_plan, intent, schema)
    ambiguity_penalty = len(ambiguities) * 0.1  # 10% penalty per ambiguity

    # Apply fallback penalty
    fallback_penalty = 0.0
    if execution_plan.get("base_table") and not intent.get("tables"):
        fallback_penalty = 0.15  # 15% penalty if base table was inferred
    if not intent.get("columns") and execution_plan.get("select"):
        fallback_penalty += 0.1  # Additional 10% if columns were inferred

    # Apply schema coverage bonus
    schema_coverage_bonus = 0.0
    selected_tables = set(col["table"] for col in execution_plan.get("select", []))
    intent_tables = set(intent.get("tables", []))
    if intent_tables and selected_tables == intent_tables:
        schema_coverage_bonus = 0.05  # 5% bonus if all intent tables are covered

    # Apply dropped-clause penalty. See detect_dropped_clauses: this is the term
    # that was missing entirely, and its absence is why a plan that lost its
    # GROUP BY could still be reported as high confidence.
    dropped_clauses = detect_dropped_clauses(intent, execution_plan)
    dropped_clause_penalty = len(dropped_clauses) * DROPPED_CLAUSE_PENALTY

    # Calculate final confidence with adjustments
    final_confidence = (
        base_confidence
        - ambiguity_penalty
        - fallback_penalty
        - dropped_clause_penalty
        + schema_coverage_bonus
    )
    final_confidence = max(0.0, min(1.0, final_confidence))  # Clamp between 0 and 1

    execution_plan["confidence"] = final_confidence
    execution_plan["ambiguities"] = ambiguities
    # Carried on the plan so the explanation can say *what* was dropped rather
    # than only that confidence is low.
    execution_plan["dropped_clauses"] = dropped_clauses
    _bound_unlimited_read(execution_plan)

    # NEW: Cache the execution plan with schema awareness and versioning
    if cache:
        cache_key = f"{PLAN_VERSION}:{str(intent)}"
        cache.set("plan", cache_key, execution_plan, ttl=600, schema_hash=schema_hash)

    # Final safety check: ensure execution_plan is a dict. STRICT_MODE turns this
    # contract violation into a raise; otherwise return a safe empty plan.
    if not isinstance(execution_plan, dict):
        fail_contract(f"Final execution_plan is not a dict: {type(execution_plan)}")
        logger.error(f"Final execution_plan is not a dict: {type(execution_plan)}")
        return {
            "base_table": None,
            "joins": [],
            "select": [],
            "where": [],
            "group_by": [],
            "order_by": None,
            "limit": None,
            "confidence": 0.0,
            "ambiguities": []
        }

    return execution_plan


def _resolve_grouping_anchor(intent: Dict[str, Any], schema: Dict, base_table: str,
                             primary_keys: Dict = None) -> List[Dict]:
    """Resolve grouping anchor to specific column.

    Converts "group by user" to "group by users.<key>".

    Args:
        intent: Structured intent
        schema: Database schema
        base_table: Base table for the query
        primary_keys: {table: [pk_col, ...]} declared keys, when available

    Returns:
        List of group by specifications
    """
    group_by = []
    group_entity = intent["group_by"]

    if not group_entity:
        return group_by

    # Prefer the declared key (or a literal ``id``), then a ``name`` column, then
    # the first column. The declared-key step is what lets a keyless-by-``id``
    # table (``departments`` keyed on ``dept_no``) group on a real column.
    if base_table in schema:
        anchor = _key_column(base_table, schema, primary_keys)
        if anchor:
            group_by.append({"table": base_table, "column": anchor})
        elif "name" in schema[base_table]:
            group_by.append({"table": base_table, "column": "name"})
        else:
            group_by.append({"table": base_table, "column": schema[base_table][0]})

    return group_by


def _resolve_ordering(intent: Dict[str, Any], schema: Dict, base_table: str, select_items: List[Dict], primary_keys: Dict = None) -> Dict:
    """Resolve ordering with aggregation context.

    If there's aggregation, order by the aggregation result, not the raw column.

    Args:
        intent: Structured intent
        schema: Database schema
        base_table: Base table for the query
        select_items: Select items from execution plan

    Returns:
        Order by specification
    """
    if not intent["order_by"]:
        return None

    def _direction(spec, default="ASC"):
        if isinstance(spec, dict):
            return spec.get("direction", default)
        if isinstance(spec, list) and spec and isinstance(spec[0], dict):
            return spec[0].get("direction", default)
        return default

    # If the plan aggregates, order by the aggregated measure. Keyed off the
    # *select items*, not ``intent["aggregation"]``: a ranking COUNT ("top 5
    # departments by headcount") is inferred during planning and never appears on
    # the intent, so gating on the intent left the ORDER BY to fall through to the
    # ``base_table.id`` default below — a column the schema need not have.
    if select_items:
        for item in select_items:
            if item.get("aggregation"):
                table = item.get("table", "")
                col_ref = f"{table}.{item['column']}" if table else item["column"]
                return {
                    "column": f"{item['aggregation']}({col_ref})",
                    "direction": _direction(intent["order_by"], "DESC"),
                }

    # Otherwise order by the named column, or the base table's key as a last
    # resort — never a hard-coded ``id`` the schema may not define.
    fallback = _key_column(base_table, schema, primary_keys)
    if not fallback and isinstance(schema, dict) and schema.get(base_table):
        fallback = schema[base_table][0]
    default_ref = f"{base_table}.{fallback}" if fallback else base_table

    order_spec = intent["order_by"]
    if isinstance(order_spec, dict):
        column = order_spec.get("column") or default_ref
        direction = order_spec.get("direction", "ASC")
    elif isinstance(order_spec, list) and len(order_spec) > 0:
        first_item = order_spec[0]
        if isinstance(first_item, dict):
            column = first_item.get("column") or default_ref
            direction = first_item.get("direction", "ASC")
        else:
            column = str(first_item)
            direction = "ASC"
    else:
        column = str(order_spec)
        direction = "ASC"

    return {
        "column": column,
        "direction": direction
    }


def _detect_ambiguities(execution_plan: Dict[str, Any], intent: Dict[str, Any], schema: Dict) -> List[str]:
    """Detect ambiguities in the execution plan.

    Identifies cases where multiple interpretations are possible.

    Enhanced ambiguity detection for vague queries.
    Detects:
    - Vague queries like "user data" without specific columns
    - Ambiguous "per" queries (count vs distribution)
    - Ambiguous "top" queries without explicit metric
    - Low confidence interpretations

    Args:
        execution_plan: Current execution plan
        intent: Original intent
        schema: Database schema

    Returns:
        List of ambiguity descriptions
    """
    ambiguities = []
    original_query = intent.get("original_query", "").lower()

    # Several columns described the question equally well and table order broke
    # the tie (``total price`` → o_totalprice / p_retailprice / l_extendedprice).
    # Answering with one is defensible; answering with *certainty* is not.
    tied = intent.get("_measure_ambiguity") or []
    if tied:
        chosen = (intent.get("aggregation") or {}).get("column") or {}
        chosen_name = f"{chosen.get('table')}.{chosen.get('column')}"
        ambiguities.append(
            "Multiple aggregation targets - used " + chosen_name + "; "
            + ", ".join(tied[:3]) + " match the question just as well")

    # Detect vague queries (e.g. "show me user data", "user information").
    # Only genuinely vague *nouns* count as ambiguous — ordinary verbs like
    # show/list/get are exactly how a valid "select all rows" is phrased and are
    # not ambiguous on their own. An explicit "all/every/entire/full" means the
    # user wants every column, which is a complete, unambiguous request.
    if len(intent.get("columns", [])) == 0 and len(intent.get("tables", [])) > 0:
        vague_nouns = ["data", "information", "stuff"]
        explicit_all = any(w in original_query for w in ["all ", "every ", "entire", "full "])
        if not explicit_all and any(noun in original_query for noun in vague_nouns):
            ambiguities.append(f"Query is vague - did you mean all columns from {intent['tables'][0]} or specific fields?")

    # Detect ambiguous "per" queries (count vs distribution)
    # "users per country" could mean count of users per country OR distribution of users
    if "per" in original_query and not intent.get("aggregation"):
        ambiguities.append("Ambiguous 'per' query - did you want a count or distribution?")

    # Detect ambiguous "top" queries without explicit metric
    # "top users" is ambiguous - top by what metric?
    if any(word in original_query for word in ["top", "highest", "most", "best"]):
        if not intent.get("aggregation"):
            ambiguities.append("Ambiguous ranking - did you want top by revenue, orders, or another metric?")

    # Detect ambiguous aggregation targets
    if intent["aggregation"] and not intent["columns"]:
        tables = intent["tables"]
        numeric_columns = []
        for table in tables:
            if table in schema:
                for col in schema[table]:
                    if any(numeric_kw in col.lower() for numeric_kw in ["amount", "price", "duration", "value", "count"]):
                        numeric_columns.append(f"{table}.{col}")

        if len(numeric_columns) > 1:
            ambiguities.append(f"Multiple aggregation targets possible: {numeric_columns}")

    # Detect a column name that several tables carry.
    #
    # "average unit price" is answerable from `products.unit_price` or
    # `order_items.unit_price` and the two give different numbers; the planner
    # picks one, which is fine, but reporting it as settled is not. Only counts
    # when the question named no table of its own — once the user says "product
    # unit price" the resolution is theirs, not a guess.
    # The table is "named" only if the user's own words contain it — the intent's
    # table list is itself inferred, so trusting it would let the guess vouch for
    # the guess and no shared column would ever be reported.
    def user_named(table: str) -> bool:
        low = table.lower()
        return low in original_query or low.rstrip("s") in original_query

    for item in execution_plan.get("select") or []:
        if not isinstance(item, dict):
            continue
        column, table = item.get("column"), item.get("table")
        if not column or not table or user_named(str(table)):
            continue
        owners = [t for t, cols in schema.items()
                  if any(c.lower() == str(column).lower() for c in cols)]
        if len(owners) > 1:
            ambiguities.append(
                f"Column '{column}' exists on {sorted(owners)}; "
                f"answered from '{table}'")

    # Detect ambiguous join paths
    if len(intent["tables"]) > 2:
        ambiguities.append(f"Multiple join paths possible with tables: {intent['tables']}")

    # Detect conflicting ordering signals
    if intent["order_by"] and intent["aggregation"]:
        order_direction = intent["order_by"].get("direction", "")
        if "highest" in str(intent["order_by"]) or "most" in str(intent["order_by"]):
            if order_direction == "ASC":
                ambiguities.append("Potential conflict: ordering direction may not match 'highest/most' intent")

    # NOTE: A "low confidence" ambiguity used to be appended here based on
    # execution_plan["confidence"], but this function runs *before* the plan
    # confidence is computed, so the value was always 0.0 — every query got a
    # spurious "Low confidence interpretation (0.00)" ambiguity plus a 0.1
    # confidence penalty from it. It was also circular (confidence penalized by
    # an ambiguity derived from confidence). Removed.

    return ambiguities
