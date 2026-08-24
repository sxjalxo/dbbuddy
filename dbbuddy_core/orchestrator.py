"""Orchestrator module for main query orchestration.

This module coordinates the entire query processing pipeline, integrating
relevance checking, intent building, query planning, SQL compilation,
execution, and safety checks.

Add observability layer (request_id, timing per stage, meta).
Add compiler versioning.
"""

import time
import uuid
import traceback

from dbbuddy_core.models import DBConfig
from dbbuddy_core.query import _extract_identifiers, get_query_type
from dbbuddy_core.execution import validate_aggregation, get_dry_run_estimate

# Import new modular components
from dbbuddy_core.safety import classify_query_safety, generate_warning, WRITE_QUERIES
from dbbuddy_core.relevance import check_query_relevance, generate_term_interpretation
from dbbuddy_core.execution import compile_sql, compile_parameterized_sql, execute_query_safely, validate_query, COMPILER_VERSION, MAX_ROWS

# Import deterministic planner components
from dbbuddy_core.intent_builder import build_query_intent
from dbbuddy_core.query_planner import plan_query_execution, build_explanation
from dbbuddy_core.contracts import fail_contract
from dbbuddy_core.context_store import get_context
from dbbuddy_core.config import DEBUG
from dbbuddy_core.logger import get_logger, setup_logger
from dbbuddy_core.query_logger import get_query_logger
from dbbuddy_core.rate_limiter import get_rate_limiter

# Import: Self-Learning Semantic Layer
from dbbuddy_core.semantic_enhancer import enhance_query
from dbbuddy_core.learning_engine import memory_scope, update_memory
from dbbuddy_core.query_planner import PLAN_VERSION

# The response cache holds a compiled plan *and* its SQL, so it is only valid
# for the planner and compiler that produced it. Both versions therefore key the
# cache: without this, fixing a planner or compiler bug left every previously
# cached question answering with the old, broken SQL until its TTL ran out — the
# fix would look inert for five minutes and then start working, which is worse
# than either state on its own.
#
# The plan cache already does this by embedding PLAN_VERSION in its hashed value
# (query_planner). This is the same idea applied to the response cache, put in
# the key prefix so the normalization applied to the query text cannot touch it.
_QUERY_CACHE_PREFIX = f"query:{PLAN_VERSION}:{COMPILER_VERSION}"

# Setup logger
setup_logger(DEBUG)
logger = get_logger()


def _any_ai_labels(semantic: dict) -> bool:
    """True when any column in the semantic layer carries an AI-refined label.

    Computed over the full layer even though only a slice of it is returned, so
    the "AI-enhanced vs rule-based" signal does not flip based on which columns a
    particular query happened to touch.
    """
    for columns in semantic.values():
        if not isinstance(columns, dict):
            continue
        for meta in columns.values():
            if isinstance(meta, dict) and meta.get("source") == "ai":
                return True
    return False


def process_query(config: DBConfig | None = None, user_query: str = "", **kwargs):
    """Process user query through deterministic pipeline.

    Args:
        config: Database configuration
        user_query: User's natural language query (must be string)
        **kwargs: Additional parameters including clarification_response

    Returns:
        Dict with query results and metadata

    Pipeline stages:
    1. Relevance Check (vector-based)
    2. Intent Builder
    3. Query Planner (deterministic)
    4. SQL Compiler
    5. Safety Classification
    6. Execution (with validation)
    7. Response Formatting

    Add observability layer (request_id, timing per stage, meta).
    Add compiler versioning.
    """
    # Phase 7.2: Strict type contract at boundary
    if not isinstance(user_query, str):
        raise TypeError(f"user_query must be str, got {type(user_query)}")

    # Generate request_id and start timing
    request_id = str(uuid.uuid4())
    start_time = time.time()
    stage_timings = {}

    # Initialize query logger
    query_logger = get_query_logger()

    # Rate limiting — only for callers that identify themselves.
    #
    # The limiter exists to stop one *remote* client monopolizing the engine, and
    # the API path always supplies the authenticated subject (backend/main.py
    # passes `payload["sub"]`). An in-process caller — the CLI, a scheduled job,
    # `run_validation.py`, the benchmark suite, the dogfood harness — is already
    # inside the trust boundary and has no identity to key on.
    #
    # Defaulting those to a shared bucket made this a limiter on *the engine*
    # rather than on a caller: every anonymous consumer contended for one 10 req/s
    # allowance, so any batch of more than ten queries throttled itself. A
    # dashboard refresh or a validation run is not abuse. No identity, no limit;
    # the untrusted surface is the one that always has an identity to supply.
    user_id = kwargs.get("user_id") or kwargs.get("ip")
    rate_limit_error = None
    if user_id:
        allowed, rate_limit_error = get_rate_limiter(
            max_requests=10, window_seconds=1).check_rate_limit(user_id)
    else:
        allowed = True

    if not allowed:
        logger.warning(f"Rate limit exceeded for user {user_id}: {rate_limit_error}")
        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": rate_limit_error,
            "suggestion": "Please wait before making another request.",
            "confidence": "low",
            "rate_limited": True,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": 0,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION,
                "rate_limited": True
            }
        }

    # Handle config parameter
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

    # Prepared per-database context (connection, schema, semantic layer, vector
    # index, relationship graph, cache). Built once per database and reused —
    # this is what removes the multi-second per-request setup cost. The semantic
    # layer + vector index are refreshed via "Analyze Schema" (process_schema).
    # Timed like every other stage: resolving the context re-fetches the schema
    # to detect a schema change, so it is a DB round-trip on the query path — and
    # it was the one significant block the stage timings did not account for,
    # which made it invisible in exactly the contended case where it matters.
    context_start = time.time()
    ctx = get_context(config)
    stage_timings["context_ms"] = round((time.time() - context_start) * 1000, 2)
    schema = ctx.schema
    semantic = ctx.semantic
    cache = ctx.cache
    schema_hash = ctx.schema_hash
    vector_store = ctx.vector_store

    # Relevance Check (vector-based)
    relevance_start = time.time()
    relevance_check = check_query_relevance(user_query, vector_store, semantic)
    stage_timings["relevance_ms"] = round((time.time() - relevance_start) * 1000, 2)

    if not relevance_check["relevant"]:
        total_latency = round((time.time() - start_time) * 1000, 2)

        # Log failed relevance check
        query_logger.log_query({
            "request_id": request_id,
            "query": user_query,
            "sql": None,
            "success": False,
            "latency_ms": total_latency,
            "confidence": "low",
            "error": "Query not relevant",
            "query_type": "invalid",
            "schema_hash": schema_hash
        })

        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "This doesn't appear to be a database query.",
            "suggestion": "Try asking about your data (e.g., 'total sales', 'users', 'orders').",
            "relevance_check": relevance_check,
            "term_interpretations": [],
            "confidence": "low",
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }

    # Generate term interpretations
    term_interpretations = generate_term_interpretation(user_query, semantic, relevance_check)

    # Detect dangerous intent
    dangerous_keywords = {"delete", "update", "drop", "truncate", "alter", "insert"}
    user_intent_dangerous = any(keyword in user_query.lower() for keyword in dangerous_keywords)
    force_write_mode = user_intent_dangerous

    # Query cache check
    cache_start = time.time()
    cache_hit = cache.get(_QUERY_CACHE_PREFIX, user_query, normalize=True, schema_hash=schema_hash)
    stage_timings["cache_check_ms"] = round((time.time() - cache_start) * 1000, 2)

    if cache_hit:
        # The response is cached *before* execution (it holds the plan/SQL, not
        # results — results would go stale). A hit therefore only short-circuits
        # planning: for a plain auto-executable read we re-run the cached SQL so
        # the caller always gets fresh rows. Anything that needs the confirmation
        # / warning / token flow (writes, dangerous intent, reads with
        # auto-execute off) falls through to the full pipeline instead of
        # returning a pre-execution snapshot with no results.
        cached_sql = cache_hit.get("sql")
        is_plain_read = (
            cache_hit.get("query_type") == "select"
            and isinstance(cached_sql, str) and cached_sql.strip()
            and not force_write_mode
            and kwargs.get("auto_execute_reads", True)
            and classify_query_safety(cached_sql)[0] == "read"
        )
        execution = None
        if is_plain_read:
            try:
                with ctx.connection() as conn:
                    execution = execute_query_safely(conn, cached_sql)
            except Exception as e:
                logger.warning(f"Cached-query re-execution failed, falling back to full pipeline: {e}")
                execution = None

        if execution and execution.get("success"):
            total_latency = round((time.time() - start_time) * 1000, 2)
            results = execution.get("results", [])
            if not isinstance(results, list):
                results = []

            # Log cache hit
            query_logger.log_query({
                "request_id": request_id,
                "query": user_query,
                "sql": cached_sql,
                "success": True,
                "latency_ms": total_latency,
                "confidence": cache_hit.get("confidence", "unknown"),
                "error": None,
                "query_type": cache_hit.get("query_type", "unknown"),
                "schema_hash": schema_hash,
                "cached": True
            })

            return {
                **cache_hit,
                "auto_executed": True,
                "results": results,
                "cached": True,
                "cache_metadata": {
                    "query": True,
                    "retrieval": cache_hit.get("cache_metadata", {}).get("retrieval", False),
                    "plan": cache_hit.get("cache_metadata", {}).get("plan", False)
                },
                # Add observability meta
                "meta": {
                    "request_id": request_id,
                    "latency_ms": total_latency,
                    "stage_timings": stage_timings,
                    "compiler_version": COMPILER_VERSION
                }
            }
        # Not a plain read (or re-execution failed) — run the full pipeline.

    # Semantic Enhancement (before intent builder)
    # Enhance query with learned semantic mappings. A term must be learned at
    # least twice before it is applied, so one-off queries cannot bias results.
    # Learned mappings are read from — and written to — this database's own scope,
    # and the schema is passed as a second check. Both matter: scoping stops a
    # mapping crossing databases at all, and schema validation catches anything
    # left over from an older, unscoped memory file.
    memory_key = memory_scope(config)
    enhanced_query = enhance_query(user_query, threshold=2, schema=schema, scope=memory_key)

    # Intent Builder
    intent_start = time.time()
    retrieved_context = relevance_check.get("retrieved_context", [])
    try:
        query_intent = build_query_intent(
            enhanced_query, retrieved_context, vector_store, schema,
            column_types=ctx.column_types,
            column_roles=getattr(ctx, "column_roles", None),
            value_index=getattr(ctx, "value_index", None),
            primary_keys=getattr(ctx, "primary_keys", None),
        )
    except Exception as e:
        # build_query_intent raises when it can't ground the query in the schema
        # (e.g. ValueError "No tables detected from query or columns"). Return a
        # graceful "not understood" response instead of letting it surface as a
        # 500 — same contract as the relevance and planner failure paths.
        logger.info(f"Intent building could not ground query: {e}")
        stage_timings["intent_building_ms"] = round((time.time() - intent_start) * 1000, 2)
        total_latency = round((time.time() - start_time) * 1000, 2)

        query_logger.log_query({
            "request_id": request_id,
            "query": user_query,
            "sql": None,
            "success": False,
            "latency_ms": total_latency,
            "confidence": "low",
            "error": "Could not map query to schema",
            "query_type": "invalid",
            "schema_hash": schema_hash
        })

        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "I couldn't match that to any tables or columns in your database.",
            "suggestion": "Try naming a table or field from your schema (e.g., 'show all users', 'total revenue per user').",
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            "confidence": "low",
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }
    stage_timings["intent_building_ms"] = round((time.time() - intent_start) * 1000, 2)

    # Query Planner (deterministic)
    planning_start = time.time()

    # DEBUG: Print full intent before planner call
    logger.debug("%s %s", "INTENT FULL:", query_intent)

    try:
        execution_plan = plan_query_execution(query_intent, schema, vector_store, cache, schema_hash,
                                               foreign_keys=ctx.foreign_keys,
                                               relationship_graph=ctx.relationship_graph,
                                               primary_keys=getattr(ctx, "primary_keys", None),
                                               column_roles=getattr(ctx, "column_roles", None))

        # Phase 7.2: Ensure execution_plan is always a dict after assignment.
        # In STRICT_MODE this raises; otherwise it recovers leniently.
        if not isinstance(execution_plan, dict):
            fail_contract(f"ExecutionPlan must be dict, got {type(execution_plan)}")
            logger.error(f"execution_plan invalid: {type(execution_plan)}")
            execution_plan = {}
    except Exception as e:
        logger.error(f"Query planner failed: {e}")
        total_latency = round((time.time() - start_time) * 1000, 2)
        stage_timings["planning_ms"] = round((time.time() - planning_start) * 1000, 2)
        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": f"Query planning failed: {str(e)}",
            "suggestion": "Try simplifying your query or using more specific table/column names.",
            "confidence": "low",
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }
    stage_timings["planning_ms"] = round((time.time() - planning_start) * 1000, 2)

    # SQL Compiler
    compilation_start = time.time()

    # HARD ASSERTIONS: Validate execution_plan structure before compilation
    if not isinstance(execution_plan, dict):
        raise ValueError(f"Invalid execution_plan type: {type(execution_plan)}")

    for key in ["select", "where", "joins", "group_by"]:
        if not isinstance(execution_plan.get(key), list):
            raise ValueError(f"{key} is not list: {type(execution_plan.get(key))}")

    try:
        sql = compile_sql(execution_plan, engine=config.engine)

        # Phase 7.2: Validate SQL is string
        if not isinstance(sql, str):
            raise TypeError(f"SQL must be string, got {type(sql)}")

    except Exception as e:
        logger.error("FULL TRACE:")
        logger.error(traceback.format_exc())
        logger.error(f"PLAN: {execution_plan}")
        total_latency = round((time.time() - start_time) * 1000, 2)
        stage_timings["compilation_ms"] = round((time.time() - compilation_start) * 1000, 2)
        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": f"SQL compilation failed: {str(e)}",
            "suggestion": "The query planner produced an invalid execution plan. Try rephrasing your question.",
            "confidence": "low",
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }
    stage_timings["compilation_ms"] = round((time.time() - compilation_start) * 1000, 2)

    model_used = "deterministic"

    # Handle dangerous intent
    if force_write_mode:
        logger.warning(f"User intent dangerous — holding for confirmation: {user_query}")
        if not sql or sql.strip().lower() in ("unknown", "invalid", ""):
            total_latency = round((time.time() - start_time) * 1000, 2)
            return {
                "query": user_query,
                "sql": None,
                "query_type": "write",
                "auto_executed": False,
                "requires_confirmation": False,
                "error": "Could not generate a valid SQL statement for this query. Try being more specific.",
                "confidence": "low",
                "relevance_check": relevance_check,
                "term_interpretations": term_interpretations,
                # Add observability meta
                "meta": {
                    "request_id": request_id,
                    "latency_ms": total_latency,
                    "stage_timings": stage_timings,
                    "compiler_version": COMPILER_VERSION
                }
            }

    # Validate SQL
    if not sql or not sql.strip():
        logger.error("SQL compilation returned empty or None")
        total_latency = round((time.time() - start_time) * 1000, 2)
        return {
            "query": user_query,
            "sql": sql,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "SQL compilation failed. The planner could not produce a valid query. Try rephrasing your question.",
            "confidence": "low",
            "requires_confirmation": False,
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }

    sql_lower = sql.strip().lower()
    if sql_lower in ("unknown", "invalid", ""):
        logger.error(f"SQL compilation returned invalid value: {sql}")
        total_latency = round((time.time() - start_time) * 1000, 2)
        return {
            "query": user_query,
            "sql": sql,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "SQL compilation failed. The planner could not produce a valid query. Try rephrasing your question.",
            "confidence": "low",
            "requires_confirmation": False,
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }

    query_type = get_query_type(sql)

    if query_type == "invalid":
        logger.error(f"SQL compilation produced invalid query type: {sql[:100]}")
        total_latency = round((time.time() - start_time) * 1000, 2)
        return {
            "query": user_query,
            "sql": sql,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "SQL compilation failed. The planner could not produce a valid query. Try rephrasing your question.",
            "confidence": "low",
            "requires_confirmation": False,
            "relevance_check": relevance_check,
            "term_interpretations": term_interpretations,
            # Add observability meta
            "meta": {
                "request_id": request_id,
                "latency_ms": total_latency,
                "stage_timings": stage_timings,
                "compiler_version": COMPILER_VERSION
            }
        }

    # Build plan-based explanation
    intent_explanation = {
        "intent": query_intent,
        "plan": execution_plan,
        "confidence": execution_plan.get("confidence", 0.0),
        "ambiguities": execution_plan.get("ambiguities", [])
    }

    # Build explainability layer
    explanation = build_explanation(query_intent, execution_plan)

    # Schema validation
    schema_check = validate_query(sql, schema)

    # Aggregation validation
    aggregation_check = validate_aggregation(sql) if query_type == "select" and "group by" in sql.lower() else {"valid": True, "error": None, "violations": []}

    # Build semantic interpretation
    semantic_interpretation = {}
    join_reasoning = []
    # Slice of the semantic layer covering only the columns this SQL touches.
    # The full layer is one entry per column in the *database* — on a real ERP
    # schema that is the bulk of the response body, and it is serialized, cached
    # in Redis, and pushed over the wire on every single query. Consumers only
    # ever look up the columns they got back, so the slice is what gets sent;
    # `ai_labeled` below is computed over the *whole* layer so the caller can
    # still tell AI-refined labels from rule-based ones.
    semantic_slice: dict = {}

    if query_type != "invalid":
        tables_used, cols_used, joins = _extract_identifiers(sql)

        logger.debug(f"JOINS DEBUG: {joins}")

        for table in tables_used:
            if table in semantic:
                # Phase 7.2: Ensure semantic[table].get(col) is a dict before .get() calls
                table_interpretation = {}
                table_slice = {}
                for col in semantic[table]:
                    if col in cols_used:
                        col_data = semantic[table].get(col, {})
                        if isinstance(col_data, dict):
                            table_interpretation[col] = col_data.get("term", col)
                            table_slice[col] = col_data
                        else:
                            table_interpretation[col] = col
                            table_slice[col] = {"term": col}
                semantic_interpretation[table] = table_interpretation
                semantic_slice[table] = table_slice

        # Build join reasoning from relationship graph (built once in context)
        relationship_graph = ctx.relationship_graph

        for join in joins:
            if not isinstance(join, dict):
                logger.warning(f"Invalid join structure (not a dict): {join}")
                continue

            table = join.get("table", "")
            condition = join.get("condition", "")

            if table in relationship_graph:
                for fk_col, (ref_table, ref_col) in relationship_graph[table].items():
                    if fk_col in condition and ref_table in condition:
                        join_reasoning.append({
                            "relationship": f"{table}.{fk_col} → {ref_table}.{ref_col}",
                            "type": "foreign_key",
                            "inferred": True
                        })

        if not join_reasoning and len(tables_used) > 1:
            for table in tables_used:
                if table in relationship_graph:
                    for fk_col, (ref_table, ref_col) in relationship_graph[table].items():
                        if ref_table in tables_used:
                            join_reasoning.append({
                                "relationship": f"{table}.{fk_col} → {ref_table}.{ref_col}",
                                "type": "foreign_key",
                                "inferred": True
                            })

    # Build response
    response = {
        "query": user_query,
        "sql": sql,
        "query_type": query_type,
        "semantic_layer": semantic_slice,
        "ai_labeled": _any_ai_labels(semantic),
        "semantic_interpretation": semantic_interpretation,
        "intent_explanation": intent_explanation,
        "join_reasoning": join_reasoning,
        "explanation": explanation,  # Explainability Engine
        "auto_executed": False,
        "schema_validation": schema_check,
        "aggregation_validation": aggregation_check,
        "model_used": model_used,
        "relevance_check": relevance_check,
        "term_interpretations": term_interpretations,
        "confidence": execution_plan.get("confidence", 0.0),
        "ambiguities": execution_plan.get("ambiguities", []),
        # Add observability meta
        # Add schema version tracking
        "meta": {
            "request_id": request_id,
            "latency_ms": 0,  # Will be updated at final return
            "stage_timings": stage_timings,
            "compiler_version": COMPILER_VERSION,
            "schema_hash": schema_hash  # Track which schema version produced this SQL
        }
    }

    # Convert numeric confidence to string BEFORE caching, so a cached response
    # carries the same "high"/"medium"/"low" shape as a fresh one.
    if isinstance(response["confidence"], (int, float)):
        if response["confidence"] >= 0.8:
            response["confidence"] = "high"
        elif response["confidence"] >= 0.5:
            response["confidence"] = "medium"
        else:
            response["confidence"] = "low"

    # Cache result if high confidence and no ambiguities
    plan_confidence = execution_plan.get("confidence", 0.0)
    plan_ambiguities = execution_plan.get("ambiguities", [])
    if plan_confidence >= 0.7 and not plan_ambiguities:
        cache.set(_QUERY_CACHE_PREFIX, user_query, response, ttl=300, normalize=True, schema_hash=schema_hash)
        response["cache_metadata"] = {
            "query": True,
            "retrieval": False,
            "plan": False
        }
    else:
        response["cache_metadata"] = {
            "query": False,
            "retrieval": False,
            "plan": False
        }

    def _finish(resp: dict) -> dict:
        """Stamp end-to-end latency and record the outcome, then return.

        Every terminal path past compilation goes through here. Previously the
        query log was written after the execute branch, which returns from inside
        itself — so the outcome of an auto-executed SELECT, the most common query
        the system serves, was never logged at all.
        """
        resp["meta"]["latency_ms"] = round((time.time() - start_time) * 1000, 2)
        query_logger.log_query({
            "request_id": request_id,
            "query": user_query,
            "sql": sql,
            "success": resp.get("auto_executed", False),
            "latency_ms": resp["meta"]["latency_ms"],
            "confidence": resp.get("confidence", "unknown"),
            "error": resp.get("error"),
            "query_type": query_type,
            "schema_hash": schema_hash,
            "cached": False,
        })
        return resp

    # Hard Stop Execution Rule
    # Block execution when ambiguity + low confidence detected
    # This prevents dangerous UX where system says "might be wrong" but still executes.
    # It only applies to queries that change data — a read-only SELECT cannot modify
    # anything, so low confidence is surfaced as an informational note, not a block.
    plan_ambiguities = plan_ambiguities or []

    # Confidence recovery path - check if clarification was provided
    clarification_response = kwargs.get("clarification_response", None)

    if clarification_response:
        # User provided clarification - apply confidence boost and re-run
        logger.info("Clarification response received, boosting confidence for re-execution")
        plan_confidence = min(1.0, plan_confidence + 0.3)  # Boost by 30%
        plan_ambiguities = []  # Clear ambiguities after user clarification
        response["clarification_applied"] = True

    # Read-only queries never modify data, so they are not gated by the hard stop.
    modifies_data = query_type != "select" or force_write_mode

    if modifies_data and plan_confidence < 0.6 and plan_ambiguities:
        logger.warning(f"Hard stop: Low confidence ({plan_confidence:.2f}) with ambiguities detected")
        response["auto_executed"] = False
        response["requires_clarification"] = True

        # Add failure transparency to clarification response
        from dbbuddy_core.query_planner import generate_failure_transparency
        failure_transparency = generate_failure_transparency(query_intent, execution_plan)

        response["clarification"] = {
            "reason": "low_confidence_with_ambiguities",
            "confidence": plan_confidence,
            "ambiguities": plan_ambiguities,
            "message": "I need clarification to execute this query accurately.",
            "transparency": failure_transparency
        }
        return _finish(response)

    if query_type == "invalid":
        response["auto_executed"] = False

        # Distinguish between user error and internal system failure
        # Check if this is an internal validation error
        if execution_plan.get("ambiguities") and any("Internal validation error" in str(a) for a in execution_plan["ambiguities"]):
            response["warning"] = "Something went wrong while processing your request. This is not your fault - it's an internal system issue. Please try again or contact support if the issue persists."
            response["error_type"] = "internal_system_failure"
        else:
            response["warning"] = "SQL compilation failed. The planner could not produce a valid query. Try rephrasing your question."
            response["error_type"] = "user_query_error"

        return _finish(response)

    # Schema validation failure
    if not schema_check["valid"]:
        logger.warning(f"Schema validation failed: {schema_check}")
        if query_type in WRITE_QUERIES:
            logger.warning("Write query with schema validation issues - routing to confirmation")
            response["auto_executed"] = False
            response["warning"] = "Generated SQL references unknown identifiers. Please review before execution."
            response["requires_confirmation"] = True
            return _finish(response)
        hint = []
        if schema_check["unknown_tables"]:
            hint.append(f"Unknown tables: {schema_check['unknown_tables']}")
        if schema_check["unknown_columns"]:
            hint.append(f"Unknown columns: {schema_check['unknown_columns']}")
        error_hint = "; ".join(hint)
        response["auto_executed"] = False
        response["warning"] = f"Generated SQL references unknown identifiers: {error_hint}. Try rephrasing."
        return _finish(response)

    # Aggregation validation failure
    if not aggregation_check["valid"]:
        logger.warning(f"Aggregation validation failed: {aggregation_check}")
        if query_type in WRITE_QUERIES:
            logger.warning("Write query with aggregation validation issues - routing to confirmation")
            response["auto_executed"] = False
            response["warning"] = "Aggregation validation issue. Please review before execution."
            response["requires_confirmation"] = True
            return _finish(response)
        logger.warning("Aggregation issue on SELECT — downgrading confidence, continuing execution")
        response["warning"] = aggregation_check["error"]
        response["confidence"] = "low"

    # Safety classification
    safety_category, requires_confirmation = classify_query_safety(sql)
    if force_write_mode:
        requires_confirmation = True
        safety_category = "write"

    response["safety_category"] = safety_category
    response["requires_confirmation"] = requires_confirmation

    if requires_confirmation:
        response["auto_executed"] = False
        _rel_graph = ctx.relationship_graph
        response["warning"] = generate_warning(sql, relationship_graph=_rel_graph)

        with ctx.connection() as conn:
            dry_run = get_dry_run_estimate(sql, conn)
        if dry_run:
            response["dry_run"] = dry_run
            estimated = dry_run["estimated_rows"]

            if estimated == 0:
                response["warning"] += "\nThis query will affect 0 rows (no data will be changed)."
            elif estimated == 1:
                response["warning"] += "\nThis will affect 1 row."
            else:
                response["warning"] += f"\nThis will affect {estimated} rows."

            if "affected_columns" in dry_run:
                columns = dry_run["affected_columns"]
                if len(columns) == 1:
                    response["warning"] += f"\nAffected column: {columns[0]}"
                else:
                    response["warning"] += f"\nAffected columns: {', '.join(columns)}"

        return _finish(response)

    # Respect the user's "auto-execute READ queries" setting. When off, hold the
    # SELECT for confirmation with a read-specific message — it only reads data,
    # so the warning differs from the write/modify confirmation above.
    auto_execute_reads = kwargs.get("auto_execute_reads", True)
    if query_type == "select" and not auto_execute_reads:
        response["auto_executed"] = False
        response["safety_category"] = "read"
        response["warning"] = (
            "Auto-execute for read queries is off. Review this query and run it when ready — "
            "it only reads data and will not modify anything."
        )
        return _finish(response)

    # Auto-execute READ queries
    if query_type == "select":
        execution_start = time.time()

        # Phase 7.2: Validate SQL before execution
        if not isinstance(sql, str):
            return {
                "query": user_query,
                "sql": None,
                "query_type": "invalid",
                "auto_executed": False,
                "error": "Invalid SQL generated (not a string)",
                "suggestion": "The query planner produced invalid SQL. Try rephrasing your question.",
                "meta": {"latency_ms": round((time.time() - start_time) * 1000, 2)}
            }

        try:
            # Execute the parameterized form (values bound, not interpolated).
            # `sql` above stays the inlined string used for display/validation.
            exec_sql, exec_params = compile_parameterized_sql(execution_plan, engine=config.engine)
            with ctx.connection() as conn:
                execution = execute_query_safely(conn, exec_sql, exec_params)
        except Exception as e:
            logger.error(f"Execution failed: {str(e)}")
            logger.error(traceback.format_exc())
            execution = {
                "success": False,
                "error": str(e),
                "results": []
            }

        # Phase 7.2: Validate execution result type
        if not isinstance(execution, dict):
            return {
                "query": user_query,
                "sql": sql,
                "query_type": "invalid",
                "auto_executed": False,
                "error": f"Execution returned invalid type: {type(execution)}",
                "suggestion": "The execution layer returned an unexpected result type.",
                "meta": {"latency_ms": round((time.time() - start_time) * 1000, 2)}
            }

        stage_timings["execution_ms"] = round((time.time() - execution_start) * 1000, 2)

        if execution["success"]:
            response["auto_executed"] = True
            # Phase 7.2: Ensure results is a list before assignment
            results = execution.get("results", [])
            if not isinstance(results, list):
                logger.warning(f"execution results is not a list: {type(results)}")
                results = []
            response["results"] = results

            # Learning Engine - update memory from successful query
            # Extract relevance score for confidence filtering
            top_score = 0.0
            retrieved = relevance_check.get("retrieved_context") or []

            if isinstance(retrieved, list) and len(retrieved) > 0:
                first = retrieved[0]
                if isinstance(first, dict):
                    top_score = first.get("score", 0.0)
                else:
                    top_score = 0.0  # fallback

            # Update semantic memory with successful query
            try:
                update_memory(user_query, query_intent, execution_plan, success=True,
                              score=top_score, scope=memory_key)
            except Exception as e:
                logger.warning(f"Memory update failed: {e}")

            # Handle truncation warning
            if execution.get("truncated", False):
                # The total is deliberately unknown: the read stops one row past
                # the display limit rather than counting a table that may hold
                # tens of millions of rows.
                response["warning"] = (
                    f"More than {MAX_ROWS} rows matched; showing the first "
                    f"{MAX_ROWS}. Add a filter or ask for a total to narrow it.")
                response["confidence"] = "low"

            has_joins = "join" in sql.lower()
            # Phase 7.2: Ensure results is a list before checking length
            results_list = execution.get("results", [])
            if not isinstance(results_list, list):
                results_list = []
            has_results = len(results_list) > 0
            if has_joins and not has_results:
                response["confidence"] = "low"
                response["warning"] = (
                    "Query returned 0 rows — this may indicate a semantic mismatch, "
                    "an overly strict filter, or no matching data exists."
                )
            elif plan_ambiguities:
                # The plan recorded more than one defensible reading. It executed,
                # but the number is one interpretation among several and must not
                # travel to a chart or an insight as settled.
                response["confidence"] = "medium" if response["confidence"] == "high" \
                    else response["confidence"]
            # Otherwise the planner's own band stands. Overwriting it with a flat
            # "medium" here made confidence a constant for every query that ran:
            # a single-table COUNT and a four-way join onto an ambiguous column
            # reported identically, so nothing downstream could tell them apart.
            return _finish(response)

        # Transparent failure - no retry
        response["auto_executed"] = False
        response["error"] = execution["error"]
        return _finish(response)
    else:
        response["auto_executed"] = False
        # FIX: Only show modify data warning for actual modifying operations
        is_write = sql_lower.startswith(("update", "delete", "insert", "drop", "alter", "create"))
        if is_write:
            response["warning"] = "This query may modify data. Please review and approve it before execution."

    return _finish(response)
