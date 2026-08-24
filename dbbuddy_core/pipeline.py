# ── Pipeline ───────────────────────────────────────────────────────────────
import logging

import dbbuddy_core.mapping as mapping_module
from dbbuddy_core.models import DBConfig

# Phase 18.5: Import modular components
from dbbuddy_core.orchestrator import process_query as orchestrator_process_query


logger = logging.getLogger(__name__)


# Phase 18.5: Main entry point delegates to orchestrator
def process_query(config: DBConfig | None = None, user_query: str = "", **kwargs):
    """Process user query through deterministic pipeline.

    This function delegates to the orchestrator module for the actual
    implementation, maintaining backward compatibility.

    Args:
        config: Database configuration
        user_query: User's natural language query
        **kwargs: Additional configuration parameters

    Returns:
        Dict with query results and metadata
    """
    import time as _time
    from dbbuddy_core import context_store as _cs

    builds_before = _cs.builds_count()
    started = _time.time()
    result = orchestrator_process_query(config, user_query, **kwargs)
    # A query is "cold" when serving it required (re)building the context.
    cold = _cs.builds_count() > builds_before
    _cs.record_query_latency(round((_time.time() - started) * 1000, 1), cold=cold)
    return result


# Phase 20.4: Moved validate_aggregation and get_dry_run_estimate to execution.py
# to break circular import between pipeline.py and orchestrator.py
# These are now imported from dbbuddy_core.execution


def calculate_confidence(response: dict) -> str:
    """Calculate real confidence score based on multiple factors with reasoning."""
    score = 100  # Start with perfect score
    reasoning = []

    # ── Deterministic paths are always high confidence ────────────────────
    # These never hallucinate — short-circuit the scoring entirely.
    if response.get("model_used") in ("deterministic", "deterministic_intent", "semantic"):
        response["confidence_reasoning"] = [f"Deterministic SQL ({response['model_used']}) — no LLM involved"]
        return "high"

    # Deduct for model used (local is better than fallback)
    if response.get("model_used") == "nemotron":
        score -= 30
        reasoning.append("Using fallback model (Nemotron)")
    elif response.get("model_used") == "unknown":
        score -= 55
        reasoning.append("Unknown model used")

    # Deduct for schema validation issues
    if not response.get("schema_validation", {}).get("valid", True):
        score -= 30
        reasoning.append("Schema validation failed")

    # Deduct for auto-fix applied
    if response.get("auto_fixed", False):
        score -= 25
        reasoning.append("SQL was auto-fixed")

    # Deduct for execution errors
    if response.get("error"):
        score -= 40
        reasoning.append("Execution error occurred")

    # Deduct for invalid query type
    if response.get("query_type") == "invalid":
        score -= 60
        reasoning.append("Invalid query type")

    # Deduct for complex joins
    join_count = len(response.get("join_reasoning", []))
    if join_count > 2:
        score -= 10
        reasoning.append(f"Complex multi-table join ({join_count} relationships)")

    # Convert score to confidence level
    if score >= 80:
        confidence = "high"
    elif score >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    # Add reasoning to response for transparency
    if reasoning:
        response["confidence_reasoning"] = reasoning

    return confidence


def benchmark_query(config: DBConfig, user_query: str, providers: list[str] = None) -> dict:
    """Run a query across multiple providers and compare results with detailed metrics.

    Args:
        config: Database configuration
        user_query: Natural language query to test
        providers: List of providers to test (default: ["local", "nemotron"])

    Returns:
        dict with benchmark results including execution correctness, latency, accuracy
    """
    import time

    if providers is None:
        providers = ["local", "nemotron"]

    results = {}

    for provider in providers:
        start_time = time.time()

        try:
            # Temporarily override provider
            test_config = DBConfig(
                host=config.host,
                user=config.user,
                password=config.password,
                database=config.database,
                engine=config.engine,
                ai=config.ai,
                ai_provider=provider,
                fallback_provider=config.fallback_provider,
                mapping_plugin=config.mapping_plugin,
            )

            response = process_query(test_config, user_query)

            end_time = time.time()
            latency_ms = (end_time - start_time) * 1000

            # Calculate execution correctness
            execution_correctness = "correct"
            if response.get("error"):
                execution_correctness = "failed"
            elif response.get("auto_fixed"):
                execution_correctness = "fixed"

            # Calculate result correctness (basic check)
            result_correctness = "unknown"
            if response.get("auto_executed") and response.get("results"):
                result_correctness = "valid"
            elif not response.get("auto_executed"):
                result_correctness = "not_executed"

            results[provider] = {
                "success": response.get("auto_executed", False),
                "sql": response.get("sql", ""),
                "model_used": response.get("model_used", ""),
                "confidence": response.get("confidence", ""),
                "error": response.get("error", ""),
                "row_count": len(response.get("results", [])) if response.get("results") else 0,
                "query_type": response.get("query_type", ""),
                "latency_ms": round(latency_ms, 2),
                "execution_correctness": execution_correctness,
                "result_correctness": result_correctness,
                "auto_fixed": response.get("auto_fixed", False),
                "schema_valid": response.get("schema_validation", {}).get("valid", True),
            }
        except Exception as e:
            end_time = time.time()
            latency_ms = (end_time - start_time) * 1000

            results[provider] = {
                "success": False,
                "error": str(e),
                "sql": "",
                "model_used": "",
                "confidence": "low",
                "row_count": 0,
                "query_type": "invalid",
                "latency_ms": round(latency_ms, 2),
                "execution_correctness": "failed",
                "result_correctness": "error",
                "auto_fixed": False,
                "schema_valid": False,
            }

    # Calculate accuracy metrics
    successful_providers = [p for p, r in results.items() if r.get("success", False)]
    accuracy_percentage = (len(successful_providers) / len(providers)) * 100 if providers else 0

    # Calculate average latency for successful providers
    avg_latency = sum(r["latency_ms"] for r in results.values() if r.get("success", False)) / len(successful_providers) if successful_providers else 0

    return {
        "query": user_query,
        "providers_tested": providers,
        "results": results,
        "summary": {
            "total_providers": len(providers),
            "successful": len(successful_providers),
            "failed": len(providers) - len(successful_providers),
            "accuracy_percentage": round(accuracy_percentage, 1),
            "average_latency_ms": round(avg_latency, 2),
            "best_provider": min(results.items(), key=lambda x: x[1]["latency_ms"] if x[1]["success"] else float('inf'))[0] if results else None,
        }
    }


def process_schema(config: DBConfig | None = None, **kwargs):
    logger = logging.getLogger(__name__)

    if config is None:
        config = DBConfig(
            host=kwargs.get("host", "localhost"),
            user=kwargs.get("user", ""),
            password=kwargs.get("password", ""),
            database=kwargs.get("database", ""),
            port=kwargs.get("port"),
            engine=kwargs.get("engine", "mysql"),
            ai=kwargs.get("ai", False),
            ai_provider=kwargs.get("provider", kwargs.get("ai_provider", "local")),
            fallback_provider=kwargs.get("fallback_provider", "nemotron"),
            mapping_plugin=kwargs.get("mapping_plugin", "default_mapping"),
        )
    elif isinstance(config, dict):
        config = DBConfig(
            host=config.get("host", "localhost"),
            user=config.get("user", ""),
            password=config.get("password", ""),
            database=config.get("database", ""),
            port=config.get("port", kwargs.get("port")),
            engine=config.get("engine", kwargs.get("engine", "mysql")),
            ai=config.get("ai", False),
            ai_provider=config.get("ai_provider", kwargs.get("provider", "local")),
            fallback_provider=config.get("fallback_provider", kwargs.get("fallback_provider", "nemotron")),
            mapping_plugin=config.get("mapping_plugin", "default_mapping"),
        )

    if not isinstance(config, DBConfig):
        raise TypeError("config must be a DBConfig instance")

    # Load mapping plugin so the rule-based layer uses the configured plugin.
    from dbbuddy_core.plugins.loader import load_mapping_plugin
    plugin_name = config.mapping_plugin
    mapping_module._mapping_plugin = load_mapping_plugin(plugin_name)
    logger.info(f"Using mapping plugin: {plugin_name}")

    # "Analyze Schema" is the authoritative rebuild: fetch schema, run AI
    # refinement, persist the semantic layer, and (re)index the vector store.
    # Subsequent /query calls reuse this prepared context with no recompute.
    from dbbuddy_core.context_store import get_context
    try:
        ctx = get_context(config, rebuild=True)
    except Exception as exc:
        # Surface the real reason (connection refused, auth failure, missing
        # driver, unknown database, …) instead of silently returning null —
        # otherwise /analyze responds 200 with `null` and the UI shows neither
        # metadata nor an error.
        logger.error(f"Schema analysis failed: {exc}")
        from dbbuddy_core.db import DatabaseUnavailableError
        raise DatabaseUnavailableError(
            f"Could not analyze {config.engine} database "
            f"'{config.database}' on {config.host}: {exc}"
        ) from exc

    schema = ctx.schema
    semantic = ctx.semantic
    logger.info(f"Schema analyzed: {len(schema)} tables")

    # ai_used reflects whether AI actually contributed to any mapping, not just
    # whether it was requested. Columns fall back to rule-based labeling when the
    # provider is unavailable (e.g. Ollama not running, no API key), so a request
    # that produced only rule-based terms must report ai_used=False.
    ai_used = any(
        isinstance(col_info, dict) and col_info.get("source") == "ai"
        for table in semantic.values()
        for col_info in table.values()
    )
    # The providers that genuinely produced at least one term (empty when none).
    ai_providers = sorted({
        col_info.get("provider")
        for table in semantic.values()
        for col_info in table.values()
        if isinstance(col_info, dict) and col_info.get("source") == "ai" and col_info.get("provider")
    })

    # When AI was requested but produced nothing, surface why (e.g. provider
    # unreachable, bad model id) instead of failing silently to rule-based.
    ai_error = None
    if config.ai and not ai_used:
        from dbbuddy_core.ai import get_last_provider_error
        ai_error = get_last_provider_error()

    return {
        "semantic_layer": semantic,
        "metadata": {
            "database": config.database,
            "ai_used": ai_used,
            "ai_requested": config.ai,
            "ai_provider": config.ai_provider if config.ai else None,
            "ai_providers_used": ai_providers,
            "ai_error": ai_error,
        },
    }
