"""
Learning Engine for Self-Learning Semantic Layer

This module learns from successful queries to build semantic mappings
between user terms and schema elements without breaking determinism.

Learning is ASSISTIVE, not authoritative:
- Memory suggests → Intent builder decides
- Never override schema truth
- Learn slowly, not aggressively
"""

import json
import os
from typing import Dict, List, Any, Optional
from pathlib import Path

from dbbuddy_core.logger import get_logger

logger = get_logger()


# Phase 6.2: Memory bounds and pruning
MAX_MAPPINGS_PER_TERM = 3  # Prevent memory explosion


# Memory file path
MEMORY_FILE = Path(__file__).parent.parent / "memory" / "semantic_memory.json"

# On-disk format version. v1 was a single flat {term: {target: count}} map shared
# by every database this process ever touched; v2 partitions it per database.
MEMORY_VERSION = "v2"

# Scope used when a caller does not identify a database. Learning still works for
# a single-database CLI session, but nothing written here is ever visible to a
# call that *does* name a database — an unattributed mapping must not leak into a
# real one.
DEFAULT_SCOPE = "_unscoped"

_EMPTY_SCOPE = {"mappings": {}, "column_usage": {}, "table_usage": {}}


def memory_scope(config) -> str:
    """Stable identity of the database a mapping was learned from.

    Host + database + engine, matching ``context_store._db_key``. Deliberately
    **not** the schema hash: a learned term should survive an ALTER TABLE, and
    re-learning everything on each schema change would make the memory useless.
    """
    if config is None:
        return DEFAULT_SCOPE
    host = getattr(config, "host", "") or ""
    database = getattr(config, "database", "") or ""
    engine = getattr(config, "engine", "") or ""
    if not (host or database):
        return DEFAULT_SCOPE
    return f"{host}|{database}|{engine}".lower()


def _blank_memory() -> Dict[str, Any]:
    return {"version": MEMORY_VERSION, "scopes": {}}


def _migrate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a v1 file forward, **discarding** its mappings.

    v1 recorded ``term -> target`` with no record of which database taught it, so
    every entry is unattributable. Carrying them into a scope would mean guessing
    an owner, and the wrong guess is exactly the failure being fixed: a mapping
    learned against a schema with ``products.price`` was being injected into
    questions asked of a schema whose column is ``unit_price``.

    Frequency counts were pooled too, so a term seen 50 times on one database
    crossed the learning threshold instantly on every other. Dropping is the only
    honest migration; the memory rebuilds from real usage within a few queries.
    """
    if raw.get("version") == MEMORY_VERSION and "scopes" in raw:
        return raw
    dropped = len(raw.get("mappings", {}) or {})
    if dropped:
        logger.warning(
            "Semantic memory upgraded to %s; discarded %d unattributable v1 "
            "mapping(s) — they carried no record of which database taught them.",
            MEMORY_VERSION, dropped,
        )
    return _blank_memory()


def _load_file() -> Dict[str, Any]:
    if not MEMORY_FILE.exists():
        return _blank_memory()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return _migrate(json.load(f))
    except Exception as e:  # noqa: BLE001 — a corrupt memory must not break a query
        logger.error(f"Failed to load memory: {e}")
        return _blank_memory()


def load_memory(scope: str = DEFAULT_SCOPE) -> Dict[str, Any]:
    """Load the semantic memory **for one database**.

    Returns the familiar ``{"mappings", "column_usage", "table_usage"}`` shape so
    callers are unchanged; the partitioning happens underneath. Cross-database
    reads are impossible by construction rather than by convention.
    """
    scoped = _load_file().get("scopes", {}).get(scope)
    if not scoped:
        return {**{k: {} for k in _EMPTY_SCOPE}, "version": MEMORY_VERSION, "scope": scope}
    return {
        "mappings": scoped.get("mappings", {}),
        "column_usage": scoped.get("column_usage", {}),
        "table_usage": scoped.get("table_usage", {}),
        "version": MEMORY_VERSION,
        "scope": scope,
    }


def save_memory(memory: Dict[str, Any], scope: str = None) -> None:
    """Persist one database's memory, leaving every other scope untouched.

    Read-modify-write of the whole file: the scopes are small, and rewriting only
    the caller's slice is what keeps concurrent learning against database A from
    erasing database B.
    """
    scope = scope or memory.get("scope") or DEFAULT_SCOPE
    try:
        store = _load_file()
        store.setdefault("scopes", {})[scope] = {
            "mappings": memory.get("mappings", {}),
            "column_usage": memory.get("column_usage", {}),
            "table_usage": memory.get("table_usage", {}),
        }
        store["version"] = MEMORY_VERSION
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        logger.debug("MEMORY SAVED scope=%s (%d mappings)",
                     scope, len(memory.get("mappings", {})))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to save memory: {e}")


def extract_keywords(query: str) -> List[str]:
    """Extract meaningful keywords from query.

    Improved keyword extraction for Phase 6.1.
    Removes structural words and keeps only meaningful semantic signals.

    Updated to include synonym extraction strategy for revenue/payments mapping.
    Fixed to remove irrelevant stopwords like 'total', 'user', 'users'.
    Added synonym bridging for person → users, revenue → payments.

    Args:
        query: User's natural language query

    Returns:
        List of lowercase keywords
    """
    stopwords = {
        "the", "is", "of", "by", "with", "and", "or", "in", "on", "at", "to",
        "for", "from", "as", "a", "an", "top", "most", "highest", "lowest",
        "show", "get", "list", "find", "what", "how", "many", "much",
        "more", "than", "average", "count", "per", "total", "user", "users"
    }

    tokens = []

    for word in query.lower().split():
        # Skip short words
        if len(word) < 3:
            continue
        # Skip stopwords
        if word in stopwords:
            continue
        tokens.append(word)

    # Synonym learning strategy: add compound terms
    # For revenue-related terms, add payments as synonym
    if "revenue" in tokens:
        tokens.append("payments")  # Revenue often maps to payments.amount

    # Synonym bridging: person → users
    if "person" in tokens:
        tokens.append("users")  # Person often maps to users table

    return tokens


def should_learn(intent: Dict[str, Any], score: float, execution_plan: Dict[str, Any] = None, success: bool = True) -> bool:
    """Determine if query should be learned from.

    Updated to use CORRECT EXECUTION instead of STRICT CONFIDENCE.
    Learn from successful structured queries, not just AI confidence scores.

    Simplified to only require meaningful signal: aggregation + tables + success.
    Removed fallback blocking since fallback happens even for correct queries.

    Args:
        intent: Query intent
        score: Confidence score from relevance check (no longer used as gate)
        execution_plan: Execution plan for additional validation
        success: Whether query executed successfully

    Returns:
        True if should learn, False otherwise
    """
    # Only learn from successful executions
    if not success:
        return False

    # Only require meaningful signal: aggregation + tables
    has_aggregation = intent.get("aggregation") is not None
    has_tables = len(intent.get("tables", [])) > 0

    # Learn if we have aggregation and tables (regardless of fallback)
    if has_aggregation and has_tables:
        return True

    return False


def add_mapping(memory: Dict[str, Any], term: str, target: str, mapping_type: str = "column") -> None:
    """Add a semantic mapping to memory with frequency tracking and pruning.

    Phase 6.1: Frequency-based learning to prevent one-shot pollution.
    Phase 6.2: Memory pruning to prevent explosion.

    Args:
        memory: Memory dict to update
        term: User term (e.g., "revenue")
        target: Schema target (e.g., "payments.amount")
        mapping_type: Type of mapping ("column" or "table")
    """
    logger.debug(f"ADD_MAPPING CALLED: '{term}' → '{target}' ({mapping_type})")

    # Phase 6.1: Use nested dict for frequency tracking
    if term not in memory["mappings"]:
        memory["mappings"][term] = {}
        logger.debug(f"CREATED NEW TERM: '{term}'")  # Debug

    # Increment frequency counter
    if target not in memory["mappings"][term]:
        memory["mappings"][term][target] = 0
        logger.debug(f"CREATED NEW MAPPING: '{term}' → '{target}'")

    memory["mappings"][term][target] += 1
    logger.debug(f"MAPPING COUNT: '{term}' → '{target}' = {memory['mappings'][term][target]}")

    # Phase 6.2: Prune if too many mappings (keep only strongest signals)
    if len(memory["mappings"][term]) > MAX_MAPPINGS_PER_TERM:
        # Remove lowest frequency mapping
        lowest = min(memory["mappings"][term], key=memory["mappings"][term].get)
        del memory["mappings"][term][lowest]
        logger.debug(f"MEMORY PRUNED: '{term}' → {lowest} (removed to maintain cap)")

    logger.info(f"MEMORY LEARNED: '{term}' → {target} ({mapping_type}) count={memory['mappings'][term][target]}")


def get_best_mapping(term: str, memory: Dict[str, Any] = None, threshold: int = 2) -> Optional[str]:
    """Get the best mapping for a term based on frequency.

    Phase 6.1: Only return mappings that meet frequency threshold.

    Args:
        term: User term to look up
        memory: Optional memory dict (if None, loads from file)
        threshold: Minimum frequency threshold (default: 2)

    Returns:
        Best mapping if threshold met, None otherwise
    """
    if memory is None:
        memory = load_memory()

    candidates = memory["mappings"].get(term, {})

    if not candidates:
        logger.debug(f"MEMORY MISS: '{term}'")
        return None

    # Find best candidate by frequency
    best = max(candidates, key=candidates.get)
    best_count = candidates[best]

    # Only return if threshold is met
    if best_count < threshold:
        logger.debug(f"MEMORY BELOW THRESHOLD: '{term}' → {best} (count={best_count}, threshold={threshold})")
        return None

    logger.info(f"MEMORY HIT: '{term}' → {best} (count={best_count})")
    return best


def debug_memory(memory: Dict[str, Any] = None) -> None:
    """Debug helper to inspect semantic memory state.

    Phase 6.2: Memory inspection for debugging Phase 6.

    Args:
        memory: Optional memory dict (if None, loads from file)
    """
    if memory is None:
        memory = load_memory()

    print("\nSEMANTIC MEMORY:")
    print(f"Total terms: {len(memory['mappings'])}")
    print(f"Total columns tracked: {len(memory['column_usage'])}")
    print(f"Total tables tracked: {len(memory['table_usage'])}")
    print("\nMappings:")

    for term, mappings in memory["mappings"].items():
        print(f"  {term} → {mappings}")

    print("\nMost used columns:")
    sorted_columns = sorted(memory["column_usage"].items(), key=lambda x: x[1], reverse=True)[:5]
    for col, count in sorted_columns:
        print(f"  {col}: {count}")


def update_column_usage(memory: Dict[str, Any], column: str) -> None:
    """Update column usage frequency.

    Args:
        memory: Memory dict to update
        column: Column reference (e.g., "payments.amount")
    """
    if column not in memory["column_usage"]:
        memory["column_usage"][column] = 0
    memory["column_usage"][column] += 1


def update_table_usage(memory: Dict[str, Any], table: str) -> None:
    """Update table usage frequency.

    Args:
        memory: Memory dict to update
        table: Table name
    """
    if table not in memory["table_usage"]:
        memory["table_usage"][table] = 0
    memory["table_usage"][table] += 1


_DISABLED_VALUES = {"1", "true", "yes", "on"}


def learning_enabled() -> bool:
    """Whether the semantic layer may write what it learns.

    Reads still apply whatever has already been learned; this only stops writes.

    Read at call time, not import time, so a container or a test can flip it
    without re-importing. Off makes a run repeatable, which matters more often
    than it sounds: `update_memory` runs after every successful query, so asking
    the same question twice can legitimately produce two different plans, and a
    demo behaves differently on a warm instance than on a visitor's cold one.
    """
    return os.getenv("DBBUDDY_DISABLE_LEARNING", "").strip().lower() not in _DISABLED_VALUES


def update_memory(query: str, intent: Dict[str, Any], plan: Dict[str, Any], success: bool = True, score: float = 0.0,
                  scope: str = DEFAULT_SCOPE) -> None:
    """Update semantic memory from successful query execution.

    Updated to use CORRECT EXECUTION instead of STRICT CONFIDENCE.
    Passes success parameter to should_learn for proper learning trigger.

    Args:
        query: User's natural language query
        intent: Query intent
        plan: Execution plan
        success: Whether query was successful
        score: Confidence score from relevance check (no longer used as gate)
    """
    logger.debug("LEARNING TRIGGERED")  # Debug: Confirm learning is being called

    if not learning_enabled():
        logger.debug("LEARNING BLOCKED: DBBUDDY_DISABLE_LEARNING is set")
        return

    if not success:
        logger.debug("LEARNING BLOCKED: Query not successful")
        return

    # Phase 7.2: Ensure intent is a dict before .get() calls
    if not isinstance(intent, dict):
        logger.debug("LEARNING BLOCKED: intent is not a dict")
        return

    # Check if we should learn from this query with execution success validation
    if not should_learn(intent, score, plan, success):
        logger.debug("LEARNING BLOCKED: should_learn returned False")
        return

    # Load current memory for THIS database only.
    memory = load_memory(scope)

    logger.debug(f"INTENT STRUCTURE: {intent}")  # Debug: Show full intent structure
    logger.debug(f"INTENT TABLES: {intent.get('tables')}")  # Debug: Show tables specifically

    # Extract keywords from query
    keywords = extract_keywords(query)
    logger.debug(f"EXTRACTED KEYWORDS: {keywords}")  # Debug: Show extracted keywords

    # ONLY learn from aggregation target (controlled learning)
    agg = intent.get("aggregation")

    if not agg:
        logger.debug("NO AGGREGATION - skipping learning")
        return

    logger.debug(f"AGGREGATION STRUCT: {agg}")  # Debug: Show aggregation structure

    column = agg.get("column", {})
    table = column.get("table")
    col_name = column.get("column")

    if not table or not col_name:
        logger.debug("INVALID AGGREGATION COLUMN - skipping learning")
        return

    # Safety rule: Only learn from measure columns
    MEASURE_COLUMNS = {"amount", "price", "value", "total", "cost", "revenue"}

    if col_name.lower() not in MEASURE_COLUMNS:
        logger.debug(f"NOT A MEASURE COLUMN: {col_name} - skipping learning")
        return

    col_ref = f"{table}.{col_name}"
    logger.debug(f"LEARNING AGGREGATION: {col_ref}")  # Debug: Show what we're learning

    # Restrict learning to ONLY semantic measure terms to prevent corruption
    MEASURE_TERMS = {
        "revenue", "sales", "income", "earnings", "profit",
        "payments", "amount", "value", "cost", "total", "price"
    }

    # Map keywords to this column (only measure terms)
    for term in keywords:
        if term.lower() in MEASURE_TERMS:
            logger.debug(f"SAFE LEARNING: '{term}' → '{col_ref}'")
            add_mapping(memory, term, col_ref, "column")
        else:
            logger.debug(f"SKIPPED LEARNING (non-measure): '{term}'")  # Debug: Show skipped learning

    # Update usage frequency
    update_column_usage(memory, col_ref)

    # Update table usage
    update_table_usage(memory, table)

    # Save updated memory — only this database's slice is rewritten.
    save_memory(memory, scope)
