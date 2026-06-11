# ── HARDCODED_MAP ───────────────────────────────────────────────────────────
HARDCODED_MAP = {
    "amt": "value", "amount": "value", "price": "value",
    "cost": "value", "total": "value", "revenue": "value",
    "qty": "quantity", "quantity": "quantity", "count": "quantity",
    "num": "quantity", "number": "quantity",
    "name": "name", "title": "name", "label": "name",
    "date": "date", "time": "date", "created_at": "date",
    "updated_at": "date", "timestamp": "date",
    "id": "identifier", "uuid": "identifier", "key": "identifier",
    "status": "status", "state": "status", "flag": "status",
    "desc": "description", "description": "description",
    "note": "description", "comment": "description",
}

# ── Semantic_Mapper ──────────────────────────────────────────────────────────
# Load mapping plugin (will be set in main())
_mapping_plugin = None

def map_column(col_name: str) -> str:
    """Classify a column name using the active mapping plugin"""
    global _mapping_plugin
    if _mapping_plugin is None:
        from dbbuddy_core.plugins.loader import load_mapping_plugin
        _mapping_plugin = load_mapping_plugin("default_mapping")
    
    try:
        return _mapping_plugin.classify(col_name)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Plugin classification failed for '{col_name}': {str(e)}")
        return "unknown"


def map_schema(schema: dict) -> dict[str, dict[str, dict]]:
    semantic_layer = {}
    
    # Get plugin name for output
    plugin_name = type(_mapping_plugin).__name__ if _mapping_plugin else "unknown"

    for table, cols in schema.items():
        semantic_layer[table] = {}

        for col in cols:
            term = map_column(col)

            semantic_layer[table][col] = {
                "term": term,
                "source": "rule",
                "plugin": plugin_name
            }

    return semantic_layer
