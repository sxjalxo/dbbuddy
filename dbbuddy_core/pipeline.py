# ── Pipeline ───────────────────────────────────────────────────────────────
import logging

import dbbuddy_core.db as db_module
import dbbuddy_core.mapping as mapping_module
import dbbuddy_core.schema as schema_module
from dbbuddy_core.ai import ai_refine
from dbbuddy_core.models import DBConfig
from dbbuddy_core.query import execute_query, fix_sql, fix_sql_with_ai, generate_sql, get_query_type, is_ollama_running, plan_intent, safe_execute, validate_against_schema


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

    if config.ai_provider in ("local", "hybrid") and not is_ollama_running():
        logger.warning("Ollama not running. Falling back to OpenAI if available.")

    sql = generate_sql(user_query, semantic, provider=config.ai_provider, schema=schema)
    query_type = get_query_type(sql)

    # Schema validation — catch hallucinated table/column names before hitting the DB
    schema_check = validate_against_schema(sql, schema) if query_type != "invalid" else {"valid": True, "unknown_tables": [], "unknown_columns": []}

    response = {
        "query": user_query,
        "sql": sql,
        "query_type": query_type,
        "semantic_layer": semantic,
        "auto_executed": False,
        "schema_validation": schema_check,
    }

    if query_type == "invalid":
        response["auto_executed"] = False
        response["warning"] = "SQL generation failed. The model could not produce a valid query. Try rephrasing your question."
        response["confidence"] = "low"
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
        fixed_sql = fix_sql(error_hint, sql, semantic, provider=config.ai_provider)
        fixed_check = validate_against_schema(fixed_sql, schema)
        if fixed_check["valid"] and get_query_type(fixed_sql) != "invalid":
            sql = fixed_sql
            response["sql"] = sql
            response["schema_validation"] = fixed_check
            response["auto_fixed"] = True
        else:
            response["auto_executed"] = False
            response["warning"] = f"Generated SQL references unknown identifiers: {error_hint}. Try rephrasing."
            response["confidence"] = "low"
            return response

    if query_type == "select":
        execution = safe_execute(conn, sql)

        if execution["success"]:
            response["auto_executed"] = True
            response["results"] = execution["results"]
            response["confidence"] = "high"
            return response

        fixed_sql = fix_sql(execution["error"], sql, semantic, provider=config.ai_provider)
        retry_execution = safe_execute(conn, fixed_sql)

        response["auto_executed"] = True
        response["original_error"] = execution["error"]
        response["fixed_sql"] = fixed_sql

        if retry_execution["success"]:
            response["results"] = retry_execution["results"]
            response["auto_fixed"] = True
            response["confidence"] = "medium"
        else:
            response["error"] = retry_execution["error"]
            response["auto_fixed"] = False
            response["confidence"] = "low"
    else:
        response["auto_executed"] = False
        response["warning"] = "This query may modify data. Please review and approve it before execution."
        response["confidence"] = "medium"

    return response
