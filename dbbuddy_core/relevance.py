"""Relevance module for query relevance checking and term interpretation.

This module handles vector-based relevance checking and term interpretation
generation for user queries.

Replace fixed vector threshold with relative gap approach.
Add ranking validation layer.
"""


from dbbuddy_core.logger import get_logger

logger = get_logger()


def check_query_relevance(user_query: str, vector_store, semantic) -> dict:
    """Check query relevance using vector similarity with relative gap approach.

    Replace fixed threshold with relative gap approach.
    Add ranking validation layer.

    Uses relative gap between top and second results instead of fixed threshold
    to maintain robustness across different schemas and embedding distributions.

    Args:
        user_query: User's natural language query
        vector_store: VectorStore instance for semantic search
        semantic: Semantic layer mapping

    Returns:
        Dict with relevance check results:
        {
            "relevant": bool,
            "reason": str,
            "score": float,
            "retrieved_context": list,
            "relative_gap": float (if applicable)
        }
    """
    # Check relevance using vector similarity
    retrieved_context = vector_store.search(user_query, top_k=5)

    # If no matches, deem irrelevant
    if not retrieved_context or len(retrieved_context) == 0:
        logger.warning(f"VectorStore returned no results for query: '{user_query}'")
        return {
            "relevant": False,
            "reason": "no_vector_matches",
            "score": 0.0,
            "retrieved_context": []
        }

    # Use relative gap approach instead of fixed threshold
    top_score = retrieved_context[0].get("score", 0.0)

    # FIX: Suppress ranking warnings when aggregation is detected
    # Aggregation queries often have low vector similarity but are semantically correct
    from dbbuddy_core.intent_builder import AGGREGATION_KEYWORDS
    query_lower = user_query.lower()
    has_aggregation = any(word in query_lower for word in AGGREGATION_KEYWORDS)

    # Ranking validation layer - note (diagnostic) if top score is low. This is
    # not actionable on its own — a valid query the planner still accepts can score
    # low — so it's debug, not a warning.
    if top_score < 0.5 and not has_aggregation:
        logger.debug(f"Low confidence ranking: top_score={top_score} for query: '{user_query}'")

    # If only one result, use absolute threshold as fallback
    if len(retrieved_context) == 1:
        if top_score < 0.3:
            # FIX: Allow low-score queries if columns detected
            has_columns = any(item.get("type") == "column" for item in retrieved_context)
            if has_columns:
                # Accepted despite low similarity — diagnostic, not actionable.
                logger.debug(f"Low similarity but columns detected: {top_score} for query: '{user_query}' - allowing")
            else:
                # The query is actually rejected here → info (explains the outcome).
                logger.info(f"Single result with low similarity: {top_score} for query: '{user_query}'")
                return {
                    "relevant": False,
                    "reason": "low_similarity_single_result",
                    "score": top_score,
                    "retrieved_context": retrieved_context
                }
    else:
        # Use relative gap between top and second result
        second_score = retrieved_context[1].get("score", 0.0)
        relative_gap = top_score - second_score
        margin = 0.1  # Minimum gap required for confidence

        if relative_gap < margin:
            # FIX: Allow low-score queries if columns detected
            has_columns = any(item.get("type") == "column" for item in retrieved_context)
            if has_columns:
                # Accepted despite a small gap — diagnostic, not actionable.
                logger.debug(f"Small relative gap but columns detected: {relative_gap} (top={top_score}, second={second_score}) for query: '{user_query}' - allowing")
            else:
                # The query is actually rejected here → info (explains the outcome).
                logger.info(f"Small relative gap: {relative_gap} (top={top_score}, second={second_score}) for query: '{user_query}'")
                return {
                    "relevant": False,
                    "reason": "small_relative_gap",
                    "score": top_score,
                    "retrieved_context": retrieved_context,
                    "relative_gap": relative_gap
                }

    # Query is relevant
    result = {
        "relevant": True,
        "reason": "vector_similarity",
        "score": top_score,
        "retrieved_context": retrieved_context
    }

    # Add relative gap if available
    if len(retrieved_context) >= 2:
        second_score = retrieved_context[1].get("score", 0.0)
        result["relative_gap"] = top_score - second_score

    return result


def generate_term_interpretation(user_query: str, semantic, relevance_check: dict) -> list:
    """Generate term interpretation explanations for the user query.

    This function provides explanations of how the system interpreted
    terms in the user's query based on the semantic layer.

    Args:
        user_query: User's natural language query
        semantic: Semantic layer mapping
        relevance_check: Relevance check results

    Returns:
        List of term interpretation explanations
    """
    interpretations = []

    if not relevance_check.get("relevant", False):
        return interpretations

    retrieved_context = relevance_check.get("retrieved_context", [])

    # Generate interpretations based on retrieved context
    for match in retrieved_context[:5]:  # Top 5 matches
        table = match.get("table", "")
        column = match.get("column", "")
        text = match.get("text", "")
        score = match.get("score", 0.0)

        if score < 0.5:
            continue  # Skip low-confidence matches

        if table and column:
            interpretations.append({
                "term": text,
                "mapped_to": f"{table}.{column}",
                "confidence": score,
                "type": "column"
            })
        elif table:
            interpretations.append({
                "term": text,
                "mapped_to": table,
                "confidence": score,
                "type": "table"
            })

    return interpretations
