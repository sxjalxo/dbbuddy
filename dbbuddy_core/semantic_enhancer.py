"""
Semantic Enhancer for Self-Learning Semantic Layer

This module enhances user queries with learned semantic mappings
before they reach the intent builder.

Learning is ASSISTIVE, not authoritative:
- Memory suggests → Intent builder decides
- Never override schema truth
- Learn slowly, not aggressively
"""

from typing import Dict, List, Any, Optional

from dbbuddy_core.learning_engine import load_memory, get_best_mapping
from dbbuddy_core.logger import get_logger

logger = get_logger()


# Lookup-time synonyms for measure terms. When one of these appears in a query
# with no learned mapping of its own, it reuses the canonical term's mapping
# (e.g. "revenue" reuses what was learned for "payments"). The memory hit is
# still attributed to the term the user actually typed.
MEASURE_SYNONYMS = {
    "revenue": "payments",
    "sales": "payments",
    "income": "payments",
    "earnings": "payments",
    "profit": "payments",
}

# Table-name synonyms: inject the canonical table word so the intent builder can
# detect the table. These map to tables, not measure columns.
TABLE_SYNONYMS = {
    "person": "users",
    "people": "users",
}


def _term_variants(term: str) -> List[str]:
    """Return the term plus its singular/plural variant for tolerant matching.

    Memory keys are stored exactly as the query term that was learned (e.g.
    "payments"), so "payment" must also probe "payments" to find it.
    """
    variants = [term]
    if term.endswith("s") and len(term) > 1:
        variants.append(term[:-1])      # payments -> payment
    else:
        variants.append(term + "s")     # payment  -> payments
    return variants


def _candidate_terms(token: str) -> List[str]:
    """Ordered, de-duplicated lookup candidates for a token: the token itself,
    its singular/plural form, its measure synonym, and that synonym's forms."""
    candidates: List[str] = []

    def add(term: str) -> None:
        for variant in _term_variants(term):
            if variant not in candidates:
                candidates.append(variant)

    add(token)
    for variant in _term_variants(token):
        if variant in MEASURE_SYNONYMS:
            add(MEASURE_SYNONYMS[variant])
    return candidates


def _resolve_mapping(token: str, memory: Dict[str, Any], threshold: int) -> Optional[str]:
    """Resolve a token to a learned mapping, tolerating singular/plural forms
    and measure synonyms. Returns the mapping target or None."""
    for candidate in _candidate_terms(token):
        mapping = get_best_mapping(candidate, memory, threshold)
        if mapping:
            return mapping
    return None


def _injection_tokens(mapping: str) -> List[str]:
    """Tokens to inject for a resolved mapping: the full column reference (drives
    aggregation inference) and its table component (drives table detection)."""
    tokens = [mapping]
    if "." in mapping:
        table = mapping.split(".", 1)[0]
        if table:
            tokens.append(table)
    return tokens


def _injection_is_valid(inject: str, schema: Dict[str, Any]) -> bool:
    """Whether an injected token names something that exists in *this* schema.

    Learned mappings are stored globally per term, not per database (see
    ``learning_engine``), so a term learned against one customer's schema is
    injected into questions asked against every other one. That is how
    ``price -> products.price`` — correct somewhere — reached a schema whose
    column is ``unit_price``, and the injected reference then drove the planner to
    a column that does not exist. The SQL compiled and the database rejected it.

    Enhancement is assistive by contract, so an injection that does not resolve
    here is simply dropped: the unenhanced query is always a valid starting point,
    and a wrong hint is worse than no hint.
    """
    if not schema:
        return True  # nothing to validate against; behave as before

    lowered = {t.lower(): {c.lower() for c in cols} for t, cols in schema.items()}
    token = inject.lower()
    if "." in token:
        table, _, column = token.partition(".")
        return column in lowered.get(table, set())
    # A bare token is a table hint; keep it only if the table is real.
    return token in lowered


def enhance_query(query: str, memory: Dict[str, Any] = None, threshold: int = 2,
                  schema: Dict[str, Any] = None, scope: str = None) -> str:
    """Enhance query with learned semantic mappings.

    Phase 6.1: Uses frequency-based mapping with threshold to prevent pollution.

    This function augments the user's query with learned term-to-schema
    mappings before it reaches the intent builder. The enhancement is
    assistive, not authoritative - the intent builder still makes the
    final decision.

    Updated to APPEND mappings into query instead of replacing tokens.
    This ensures semantic meaning is injected early in the pipeline.

    Example:
        Input: "revenue per user"
        After enhancement: "revenue per user payments.amount"

    Args:
        query: User's natural language query
        memory: Optional memory dict (if None, loads from file)
        threshold: Minimum frequency threshold for mappings (default: 2)

    Returns:
        Enhanced query with semantic mappings appended
    """
    logger.debug(f"ENHANCER CALLED: '{query}'")  # Debug: Confirm enhancer is running

    if memory is None:
        # Only this database's learned mappings. Schema validation below is the
        # second line of defence; scoping is the first — a term that means
        # different things in two schemas resolves to a real column in both, so
        # validation alone would happily inject the wrong one.
        from dbbuddy_core.learning_engine import DEFAULT_SCOPE

        memory = load_memory(scope or DEFAULT_SCOPE)

    if not memory.get("mappings"):
        # No learned mappings yet - nothing to enhance.
        logger.debug("MEMORY EMPTY: No mappings available for enhancement")
        return query

    # Match against lowercased tokens, but only ever APPEND injected mappings to
    # the ORIGINAL query — never rebuild it. Rebuilding from lowercased tokens
    # destroyed the query's capitalization, which the intent builder relies on to
    # spot a name literal ("age of Carol" → carol → no WHERE). Enhancement is
    # additive by contract; the original text must survive untouched.
    tokens = query.lower().split()
    seen = set(tokens)
    appended: List[str] = []

    # Table-name synonyms: inject the canonical table word so the intent builder
    # can detect the table (these map to tables, not measure columns).
    for synonym, table in TABLE_SYNONYMS.items():
        if synonym in tokens and table not in seen and _injection_is_valid(table, schema):
            appended.append(table)
            seen.add(table)

    mappings_found = []
    for token in tokens:
        # Resolve via learned memory, tolerating singular/plural and synonyms.
        mapping = _resolve_mapping(token, memory, threshold)

        if mapping:
            # Attribute the hit to the term the user actually typed.
            logger.debug(f"MEMORY HIT: '{token}' → '{mapping}'")
            for inject in _injection_tokens(mapping):
                if inject in seen:
                    continue
                if not _injection_is_valid(inject, schema):
                    logger.debug(
                        "DROPPED INJECTION: '%s' → '%s' does not exist in this schema",
                        token, inject)
                    continue
                appended.append(inject)
                seen.add(inject)
            mappings_found.append((token, mapping))

    enhanced_query = query if not appended else query + " " + " ".join(appended)

    if mappings_found or appended:
        logger.info(f"QUERY ENHANCED: '{query}' → '{enhanced_query}'")
    else:
        logger.debug(f"QUERY NOT ENHANCED: '{query}' (no applicable mappings)")

    return enhanced_query
