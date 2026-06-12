# ── Pipeline ───────────────────────────────────────────────────────────────
import logging
import re

import dbbuddy_core.db as db_module
import dbbuddy_core.mapping as mapping_module
import dbbuddy_core.schema as schema_module
from dbbuddy_core.ai import ai_refine
from dbbuddy_core.models import DBConfig
from dbbuddy_core.query import _extract_identifiers, execute_query, fix_sql, fix_sql_with_ai, generate_sql, get_query_type, is_ollama_running, plan_intent, safe_execute, validate_against_schema


# Query categories for safety
READ_QUERIES = {"select", "show", "describe", "explain"}
WRITE_QUERIES = {"insert", "update", "delete", "drop", "alter", "truncate", "grant", "revoke", "create"}


def classify_query_safety(sql: str) -> tuple[str, bool]:
    """Classify query as READ or WRITE and whether it requires confirmation.
    
    Handles edge cases:
    - Multi-statement queries (treated as write)
    - CTE with modification (treated as write)
    - Sneaky injection attempts (treated as write)
    
    Returns:
        tuple: (category, requires_confirmation)
        category: "read" or "write"
        requires_confirmation: bool
    """
    sql_lower = sql.lower().strip()
    
    # Check for multiple statements (semicolon separated)
    if ";" in sql_lower and sql_lower.count(";") > 1:
        return "write", True  # Multi-statement queries treated as write
    
    # Check for CTE with write operations (WITH ... DELETE/UPDATE/INSERT)
    if sql_lower.startswith("with") and any(op in sql_lower for op in ["delete", "update", "insert"]):
        return "write", True
    
    # Check for sneaky injection attempts (SELECT followed by DROP)
    if "select" in sql_lower and any(op in sql_lower for op in ["drop", "delete", "truncate"]):
        return "write", True
    
    # Extract the first keyword
    first_word = sql_lower.split()[0] if sql_lower.split() else ""
    
    if first_word in READ_QUERIES:
        return "read", False
    elif first_word in WRITE_QUERIES:
        return "write", True
    else:
        # Default to write for safety
        return "write", True


def generate_warning(sql: str) -> str:
    """Generate intelligent warning for write operations."""
    sql_lower = sql.lower()
    
    # Extract table name for more specific warnings
    table_match = re.search(r'from\s+(\w+)|into\s+(\w+)|table\s+(\w+)', sql_lower)
    table_name = table_match.group(1) or table_match.group(2) or table_match.group(3) if table_match else "the database"
    
    if "drop" in sql_lower and "table" in sql_lower:
        return f"🚨 This query will DROP the table '{table_name}'. All data will be permanently lost and cannot be recovered."
    elif "drop" in sql_lower:
        return f"🚨 This query will DROP '{table_name}'. This action cannot be undone."
    elif "delete" in sql_lower:
        return f"⚠️ This query will DELETE rows from '{table_name}'. This action cannot be undone."
    elif "truncate" in sql_lower:
        return f"🚨 This query will TRUNCATE the table '{table_name}', removing all data instantly."
    elif "update" in sql_lower:
        return f"⚠️ This query will UPDATE existing records in '{table_name}'. Ensure conditions are correct to avoid unintended changes."
    elif "insert" in sql_lower:
        return f"⚠️ This query will INSERT new data into '{table_name}'."
    elif "alter" in sql_lower:
        return f"⚠️ This query will ALTER the structure of '{table_name}'. This may affect existing data and applications."
    elif "grant" in sql_lower or "revoke" in sql_lower:
        return f"⚠️ This query will modify access permissions. This affects database security."
    elif "create" in sql_lower:
        return f"⚠️ This query will CREATE a new object in the database."
    
    return "⚠️ This query may modify the database structure or data."


def generate_count_query(sql: str) -> str | None:
    """Generate a COUNT query from DELETE/UPDATE statements for dry run preview.
    
    Args:
        sql: DELETE or UPDATE SQL statement
        
    Returns:
        SELECT COUNT(*) query with same WHERE clause, or None if not applicable
    """
    sql_lower = sql.lower().strip()
    
    # Handle DELETE FROM table WHERE conditions
    if sql_lower.startswith("delete"):
        # Extract table name and WHERE clause
        match = re.search(r'delete\s+from\s+(\w+)\s+where\s+(.+)', sql_lower, re.IGNORECASE)
        if match:
            table = match.group(1)
            where_clause = match.group(2)
            return f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
        
        # DELETE without WHERE (all rows)
        match = re.search(r'delete\s+from\s+(\w+)', sql_lower, re.IGNORECASE)
        if match:
            table = match.group(1)
            return f"SELECT COUNT(*) FROM {table}"
    
    # Handle UPDATE table SET ... WHERE conditions
    elif sql_lower.startswith("update"):
        # Extract table name and WHERE clause
        match = re.search(r'update\s+(\w+)\s+set\s+.+\s+where\s+(.+)', sql_lower, re.IGNORECASE)
        if match:
            table = match.group(1)
            where_clause = match.group(2)
            return f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
        
        # UPDATE without WHERE (all rows)
        match = re.search(r'update\s+(\w+)\s+set\s+', sql_lower, re.IGNORECASE)
        if match:
            table = match.group(1)
            return f"SELECT COUNT(*) FROM {table}"
    
    return None


def extract_affected_columns(sql: str) -> list[str]:
    """Extract affected columns from UPDATE statements.
    
    Args:
        sql: UPDATE SQL statement
        
    Returns:
        List of column names being modified
    """
    sql_lower = sql.lower().strip()
    
    if not sql_lower.startswith("update"):
        return []
    
    # Extract SET clause
    match = re.search(r'update\s+\w+\s+set\s+(.+?)(?:\s+where|$)', sql_lower, re.IGNORECASE)
    if not match:
        return []
    
    set_clause = match.group(1)
    
    # Parse column names from SET clause
    # Handle: col1 = val1, col2 = val2
    columns = []
    for assignment in set_clause.split(','):
        assignment = assignment.strip()
        if '=' in assignment:
            column = assignment.split('=')[0].strip()
            # Remove any function calls or expressions
            column = re.sub(r'\([^)]*\)', '', column)
            column = column.strip()
            if column:
                columns.append(column)
    
    return columns


def generate_term_interpretation(query: str, semantic: dict, relevance_check: dict) -> list:
    """Generate explanations for how query terms were interpreted from the semantic layer.
    
    Args:
        query: User's natural language query
        semantic: Semantic layer mapping from schema
        relevance_check: Result from is_query_relevance check
        
    Returns:
        list of interpretation explanations (e.g., "revenue → orders.total_amount")
    """
    interpretations = []
    
    if not relevance_check["matched_terms"]:
        return interpretations
    
    query_lower = query.lower()
    
    # For each matched term, find its semantic mapping
    for term in relevance_check["matched_terms"]:
        # Skip common words
        common_words = {'show', 'get', 'give', 'tell', 'last', 'next', 'previous', 'current'}
        if term in common_words:
            continue
        
        # Find the semantic mapping for this term
        for table_name, columns in semantic.items():
            # Check if term matches a table name
            if term == table_name.lower():
                interpretations.append({
                    "term": term,
                    "mapped_to": f"table: {table_name}",
                    "type": "table"
                })
                continue
            
            # Check if term matches a column name or its mapped term
            for col_name, col_info in columns.items():
                if term == col_name.lower():
                    interpretations.append({
                        "term": term,
                        "mapped_to": f"{table_name}.{col_name}",
                        "type": "column"
                    })
                    break
                elif "term" in col_info and term == col_info["term"].lower():
                    interpretations.append({
                        "term": term,
                        "mapped_to": f"{table_name}.{col_name}",
                        "type": "semantic_mapping"
                    })
                    break
    
    return interpretations


def is_query_relevant(query: str, semantic: dict) -> dict:
    """Detect if a query is relevant to the database schema.
    
    Uses weighted scoring to check if the query contains terms that
    overlap with the semantic layer (table names, column names, mapped terms).
    
    Weights:
    - Table matches: 2.0 points (strong signal)
    - Column matches: 1.5 points (medium signal)
    - Semantic matches: 1.0 point (weaker signal)
    
    Args:
        query: User's natural language query
        semantic: Semantic layer mapping from schema
        
    Returns:
        dict with 'relevant' (bool), 'confidence' (float), 'matched_terms' (list), and 'score' (float)
    """
    import re
    from collections import Counter
    
    # Normalize query to lowercase and extract tokens
    query_lower = query.lower()
    tokens = re.findall(r'\b\w+\b', query_lower)
    
    # Build semantic term sets by type
    table_terms = set()
    column_terms = set()
    semantic_terms = set()
    
    for table_name, columns in semantic.items():
        table_terms.add(table_name.lower())
        for col_name, col_info in columns.items():
            column_terms.add(col_name.lower())
            if "term" in col_info:
                semantic_terms.add(col_info["term"].lower())
    
    # Find matches by type with weights
    table_matches = []
    column_matches = []
    semantic_matches = []
    
    for token in tokens:
        if token in table_terms:
            table_matches.append(token)
        elif token in column_terms:
            column_matches.append(token)
        elif token in semantic_terms:
            semantic_matches.append(token)
    
    # Remove duplicates while preserving order
    table_matches = list(dict.fromkeys(table_matches))
    column_matches = list(dict.fromkeys(column_matches))
    semantic_matches = list(dict.fromkeys(semantic_matches))
    
    # Calculate weighted score
    score = (
        2.0 * len(table_matches) +
        1.5 * len(column_matches) +
        1.0 * len(semantic_matches)
    )
    
    # All matched terms combined
    all_matches = table_matches + column_matches + semantic_matches
    all_matches = list(dict.fromkeys(all_matches))
    
    # Calculate relevance based on weighted score AND coverage
    # Coverage: percentage of tokens that matched schema terms
    coverage = len(all_matches) / max(token_count, 1) if token_count > 0 else 0
    
    # Threshold: 1.5 for short queries, 2.0 for longer queries
    # Coverage threshold: at least 20% of tokens should be relevant
    threshold = 1.5 if token_count <= 6 else 2.0
    coverage_threshold = 0.2
    relevant = score >= threshold and coverage >= coverage_threshold
    
    # Calculate confidence based on score and query length
    max_possible_score = 2.0 * token_count  # If all tokens were table matches
    confidence = score / max_possible_score if max_possible_score > 0 else 0
    
    # Boost confidence if matches are significant terms (not common words)
    common_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                   'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
                   'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
                   'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
                   'from', 'as', 'into', 'through', 'during', 'before', 'after',
                   'above', 'below', 'between', 'under', 'again', 'further', 'then',
                   'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all',
                   'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no',
                   'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
                   'just', 'and', 'but', 'if', 'or', 'because', 'until', 'while',
                   'although', 'though', 'since', 'so', 'that', 'this', 'these',
                   'those', 'what', 'which', 'who', 'whom', 'whose', 'show', 'get',
                   'give', 'tell', 'ask', 'last', 'next', 'previous', 'current'}
    
    significant_matches = [m for m in all_matches if m not in common_words]
    if significant_matches:
        confidence = min(confidence + 0.2, 1.0)
    
    return {
        "relevant": relevant,
        "confidence": confidence,
        "score": score,
        "coverage": coverage,
        "matched_terms": all_matches,
        "significant_matches": significant_matches,
        "table_matches": table_matches,
        "column_matches": column_matches,
        "semantic_matches": semantic_matches
    }


def validate_aggregation(sql: str) -> dict:
    """Validate GROUP BY aggregation rules.
    
    Ensures that all non-aggregated columns in SELECT are either:
    - In the GROUP BY clause, OR
    - Used with an aggregate function (SUM, COUNT, AVG, MAX, MIN, etc.)
    
    Args:
        sql: SQL statement to validate
        
    Returns:
        dict with 'valid' (bool), 'error' (str if invalid), and 'violations' (list of invalid columns)
    """
    sql_lower = sql.lower().strip()
    
    # Extract SELECT clause
    select_match = re.search(r'select\s+(.+?)\s+from', sql_lower, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return {"valid": True, "error": None, "violations": []}
    
    select_clause = select_match.group(1)
    
    # Extract GROUP BY clause
    group_by_match = re.search(r'group by\s+(.+?)(?:\s+having|\s+order by|\s+limit|$)', sql_lower, re.IGNORECASE)
    group_by_columns = []
    if group_by_match:
        group_by_clause = group_by_match.group(1)
        # Split by comma and clean up
        group_by_columns = [col.strip() for col in group_by_clause.split(',')]
    
    # Aggregate functions to detect
    aggregate_functions = ['sum', 'count', 'avg', 'max', 'min', 'stddev', 'variance', 'group_concat']
    
    # Extract columns from SELECT clause
    # Remove subqueries, handle aliases
    select_columns = []
    for col in select_clause.split(','):
        col = col.strip()
        # Remove aliases (AS or space before alias)
        col = re.sub(r'\s+as\s+.*$', '', col, flags=re.IGNORECASE)
        col = re.sub(r'\s+\w+\s*$', '', col)  # Remove trailing alias
        col = col.strip()
        
        # Check if it's an aggregate function
        is_aggregate = any(func in col for func in aggregate_functions)
        
        if not is_aggregate and col and col != '*':
            select_columns.append(col)
    
    # Check for violations
    violations = []
    for col in select_columns:
        # Clean up column name (remove table prefix, functions, etc.)
        col_clean = col
        
        # Remove table prefix (e.g., "u.name" -> "name")
        if '.' in col_clean:
            col_clean = col_clean.split('.')[-1]
        
        # Remove any remaining function calls or expressions
        col_clean = re.sub(r'\([^)]*\)', '', col_clean)
        col_clean = col_clean.strip()
        
        # Check if this column is in GROUP BY
        in_group_by = any(
            col_clean in group_col or group_col.endswith(f'.{col_clean}') or group_col == col
            for group_col in group_by_columns
        )
        
        if not in_group_by:
            violations.append(col)
    
    if violations:
        return {
            "valid": False,
            "error": f"GROUP BY violation: Non-aggregated columns not in GROUP BY clause: {', '.join(violations)}",
            "violations": violations
        }
    
    return {"valid": True, "error": None, "violations": []}


def get_dry_run_estimate(sql: str, conn) -> dict:
    """Execute dry run count query to estimate impact.
    
    Args:
        sql: DELETE or UPDATE SQL statement
        conn: Database connection
        
    Returns:
        dict with 'count_query', 'estimated_rows', and 'affected_columns' or None if not applicable
    """
    count_query = generate_count_query(sql)
    if not count_query:
        return None
    
    try:
        result = safe_execute(conn, count_query)
        if result["success"] and result["results"]:
            estimated_rows = result["results"][0].get("COUNT(*)", 0) if isinstance(result["results"][0], dict) else result["results"][0][0]
            
            dry_run_info = {
                "count_query": count_query,
                "estimated_rows": estimated_rows
            }
            
            # Add affected columns for UPDATE queries
            if sql.lower().startswith("update"):
                affected_columns = extract_affected_columns(sql)
                if affected_columns:
                    dry_run_info["affected_columns"] = affected_columns
            
            return dry_run_info
    except Exception as e:
        logger.warning(f"Dry run count query failed: {e}")
    
    return None


def calculate_confidence(response: dict) -> str:
    """Calculate real confidence score based on multiple factors with reasoning."""
    score = 100  # Start with perfect score
    reasoning = []
    
    # Deduct for model used (local is better than fallback)
    if response.get("model_used") == "nemotron":
        score -= 20
        reasoning.append("Using fallback model (Nemotron)")
    elif response.get("model_used") == "unknown":
        score -= 50
        reasoning.append("Unknown model used")
    
    # Deduct for schema validation issues
    if not response.get("schema_validation", {}).get("valid", True):
        score -= 30
        reasoning.append("Schema validation failed")
    
    # Deduct for auto-fix applied
    if response.get("auto_fixed", False):
        score -= 15
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
            ai=config.get("ai", False),
            ai_provider=config.get("ai_provider", kwargs.get("provider", "local")),
            fallback_provider=config.get("fallback_provider", kwargs.get("fallback_provider", "nemotron")),
            mapping_plugin=config.get("mapping_plugin", "default_mapping"),
        )

    if not isinstance(config, DBConfig):
        raise TypeError("config must be a DBConfig instance")
    
    # Load mapping plugin
    from dbbuddy_core.plugins.loader import load_mapping_plugin
    plugin_name = config.mapping_plugin
    global _mapping_plugin
    _mapping_plugin = load_mapping_plugin(plugin_name)
    logger.info(f"Using mapping plugin: {plugin_name}")
    
    # Connect to database
    conn = db_module.connect_db(
        config.host,
        config.user,
        config.password,
        config.database,
    )
    if conn is None:
        logger.error("Database connection failed")
        return None
    
    logger.info("Database connection established")
    
    # Fetch schema
    schema = schema_module.fetch_schema(conn)
    if schema is None:
        logger.error("Schema fetch failed")
        return None
    
    logger.info(f"Schema fetched: {len(schema)} tables")
    
    # Map schema
    logger.info("Generating semantic layer output")
    semantic = mapping_module.map_schema(schema)
    
    # AI refinement (if enabled)
    if config.ai:
        provider = config.ai_provider
        logger.info(f"AI provider selected: {provider}")
        semantic = ai_refine(semantic, provider, schema=schema)

    return {
        "semantic_layer": semantic,
        "metadata": {
            "database": config.database,
            "ai_used": config.ai,
        },
    }


def process_query(config: DBConfig | None = None, user_query: str = "", **kwargs):
    logger = logging.getLogger(__name__)

    if config is None:
        config = DBConfig(
            host=kwargs.get("host", "localhost"),
            user=kwargs.get("user", ""),
            password=kwargs.get("password", ""),
            database=kwargs.get("database", ""),
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
            ai=config.get("ai", False),
            ai_provider=config.get("ai_provider", kwargs.get("provider", "local")),
            fallback_provider=config.get("fallback_provider", kwargs.get("fallback_provider", "nemotron")),
            mapping_plugin=config.get("mapping_plugin", "default_mapping"),
        )

    if not isinstance(config, DBConfig):
        raise TypeError("config must be a DBConfig instance")

    conn = db_module.connect_db(config.host, config.user, config.password, config.database)
    if conn is None:
        logger.error("Database connection failed")
        raise RuntimeError("Unable to connect to the database.")

    schema = schema_module.fetch_schema(conn)
    if schema is None:
        logger.error("Schema fetch failed")
        raise RuntimeError("Unable to fetch schema from the database.")

    semantic = mapping_module.map_schema(schema)
    if config.ai:
        semantic = ai_refine(semantic, provider=config.ai_provider, schema=schema)

    # Query relevance detection - filter out non-database queries
    relevance_check = is_query_relevant(user_query, semantic)
    if not relevance_check["relevant"]:
        logger.warning(f"Query deemed irrelevant to database: {user_query}")
        return {
            "query": user_query,
            "sql": None,
            "query_type": "invalid",
            "auto_executed": False,
            "error": "This doesn't appear to be a database query.",
            "suggestion": "Try asking about your data (e.g., 'total sales', 'users', 'orders').",
            "relevance_check": relevance_check,
            "confidence": "low"
        }

    # Generate term interpretation explanations
    term_interpretations = generate_term_interpretation(user_query, semantic, relevance_check)

    if config.ai_provider in ("local", "hybrid") and not is_ollama_running():
        logger.warning("Ollama not running. Falling back to Nemotron or OpenAI if available.")

    sql, model_used = generate_sql(user_query, semantic, provider=config.ai_provider, schema=schema)
    query_type = get_query_type(sql)
    
    # Generate intent explanation for the query
    intent_explanation = plan_intent(user_query, semantic, provider=config.ai_provider)

    # Schema validation — catch hallucinated table/column names before hitting the DB
    schema_check = validate_against_schema(sql, schema) if query_type != "invalid" else {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}
    
    # Aggregation validation — catch GROUP BY violations
    aggregation_check = validate_aggregation(sql) if query_type == "select" and "group by" in sql.lower() else {"valid": True, "error": None, "violations": []}

    # Add semantic interpretation using the semantic layer
    semantic_interpretation = {}
    join_reasoning = []
    
    if query_type != "invalid":
        # Extract tables, columns, and joins from the SQL
        tables_used, cols_used, joins = _extract_identifiers(sql)
        
        # Build intelligent interpretation using semantic layer
        for table in tables_used:
            if table in semantic:
                semantic_interpretation[table] = {
                    col: semantic[table].get(col, {}).get("term", col)
                    for col in semantic[table]
                    if col in cols_used
                }
        
        # Build join reasoning from relationship graph
        from dbbuddy_core.query import build_relationship_graph
        relationship_graph = build_relationship_graph(schema)
        
        for join in joins:
            table = join.get("table", "")
            condition = join.get("condition", "")
            
            # Check if this join matches a relationship in the graph
            if table in relationship_graph:
                for fk_col, (ref_table, ref_col) in relationship_graph[table].items():
                    if fk_col in condition and ref_table in condition:
                        join_reasoning.append({
                            "relationship": f"{table}.{fk_col} → {ref_table}.{ref_col}",
                            "type": "foreign_key",
                            "inferred": True
                        })
        
        # If no explicit joins found but multiple tables, add reasoning
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
    
    response = {
        "query": user_query,
        "sql": sql,
        "query_type": query_type,
        "semantic_layer": semantic,
        "semantic_interpretation": semantic_interpretation,
        "intent_explanation": intent_explanation,
        "join_reasoning": join_reasoning,
        "auto_executed": False,
        "schema_validation": schema_check,
        "aggregation_validation": aggregation_check,
        "model_used": model_used,
        "relevance_check": relevance_check,
        "term_interpretations": term_interpretations,
    }

    if query_type == "invalid":
        response["auto_executed"] = False
        response["warning"] = "SQL generation failed. The model could not produce a valid query. Try rephrasing your question."
        response["confidence"] = calculate_confidence(response)
        return response

    if not schema_check["valid"]:
        logger.warning(f"Schema validation failed: {schema_check}")
        # Attempt a fix before giving up — pass the validation failure as the error
        hint = []
        if schema_check["unknown_tables"]:
            hint.append(f"Unknown tables: {schema_check['unknown_tables']}")
        if schema_check["unknown_columns"]:
            hint.append(f"Unknown columns: {schema_check['unknown_columns']}")
        error_hint = "; ".join(hint)
        fixed_sql, fix_model = fix_sql(error_hint, sql, semantic, provider=config.ai_provider)
        fixed_check = validate_against_schema(fixed_sql, schema)
        if fixed_check["valid"] and get_query_type(fixed_sql) != "invalid":
            sql = fixed_sql
            response["sql"] = sql
            response["schema_validation"] = fixed_check
            response["auto_fixed"] = True
        else:
            response["auto_executed"] = False
            response["warning"] = f"Generated SQL references unknown identifiers: {error_hint}. Try rephrasing."
            response["confidence"] = calculate_confidence(response)
            return response

    # Aggregation validation check
    if not aggregation_check["valid"]:
        logger.warning(f"Aggregation validation failed: {aggregation_check}")
        response["auto_executed"] = False
        response["warning"] = aggregation_check["error"]
        response["confidence"] = calculate_confidence(response)
        return response

    # Safety classification: READ vs WRITE
    safety_category, requires_confirmation = classify_query_safety(sql)
    response["safety_category"] = safety_category
    response["requires_confirmation"] = requires_confirmation

    if requires_confirmation:
        # WRITE queries require user confirmation
        response["auto_executed"] = False
        response["warning"] = generate_warning(sql)
        
        # Add dry run estimate for DELETE/UPDATE queries
        dry_run = get_dry_run_estimate(sql, conn)
        if dry_run:
            response["dry_run"] = dry_run
            # Enhance warning with estimated impact
            estimated = dry_run["estimated_rows"]
            
            # Add row count information
            if estimated == 0:
                response["warning"] += f"\nℹ️ This query will affect 0 rows (no data will be changed)."
            elif estimated == 1:
                response["warning"] += f"\n⚠️ This will affect 1 row."
            else:
                response["warning"] += f"\n⚠️ This will affect {estimated} rows."
            
            # Add affected columns for UPDATE queries
            if "affected_columns" in dry_run:
                columns = dry_run["affected_columns"]
                if len(columns) == 1:
                    response["warning"] += f"\nAffected column: {columns[0]}"
                else:
                    response["warning"] += f"\nAffected columns: {', '.join(columns)}"
        
        response["confidence"] = calculate_confidence(response)
        return response

    # Auto-execute READ queries only
    if query_type == "select":
        execution = safe_execute(conn, sql)

        if execution["success"]:
            response["auto_executed"] = True
            response["results"] = execution["results"]
            response["confidence"] = calculate_confidence(response)
            return response

        fixed_sql, fix_model = fix_sql(execution["error"], sql, semantic, provider=config.ai_provider)
        retry_execution = safe_execute(conn, fixed_sql)

        response["auto_executed"] = True
        response["original_error"] = execution["error"]
        response["fixed_sql"] = fixed_sql

        if retry_execution["success"]:
            response["results"] = retry_execution["results"]
            response["auto_fixed"] = True
            
            # Silent failure detection for aggregation queries
            # Only flag if aggregation + joins + 0 rows (likely semantic issue)
            # to avoid false alarms for legitimate empty results
            has_aggregation = "group by" in sql.lower()
            has_joins = "join" in sql.lower()
            has_results = len(retry_execution["results"]) > 0
            
            if has_aggregation and has_joins and not has_results:
                logger.warning("Aggregation query with joins returned 0 rows - possible GROUP BY violation")
                response["confidence"] = "low"
                response["warning"] = "Aggregation query with joins returned 0 rows. This may indicate a GROUP BY violation or incorrect aggregation."
            response["confidence"] = calculate_confidence(response)
        else:
            response["error"] = retry_execution["error"]
            response["auto_fixed"] = False
            response["confidence"] = calculate_confidence(response)
    else:
        response["auto_executed"] = False
        response["warning"] = "This query may modify data. Please review and approve it before execution."
        response["confidence"] = calculate_confidence(response)

    return response
