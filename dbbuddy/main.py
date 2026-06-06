# ── Imports ────────────────────────────────────────────────────────────────
import os, sys, json, getpass
import logging
import requests
from dbbuddy.db import connect_db

# ── Logging Configuration ───────────────────────────────────────────────────
import os
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(log_dir, "dbbuddy.log"),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

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

# ── Schema_Fetcher ──────────────────────────────────────────────────────────
def fetch_schema(conn) -> dict[str, list[str]] | None:
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        schema = {}
        for table in tables:
            cursor.execute(f"DESCRIBE {table}")
            schema[table] = [row[0] for row in cursor.fetchall()]
        return schema
    except Exception as e:
        logger.error(f"Schema fetch failed: {str(e)}")
        print(f"[-] Failed to fetch schema: {e}")
        return None

# ── Semantic_Mapper ──────────────────────────────────────────────────────────
# Load mapping plugin (will be set in main())
_mapping_plugin = None

def map_column(col_name: str) -> str:
    """Classify a column name using the active mapping plugin"""
    global _mapping_plugin
    if _mapping_plugin is None:
        from dbbuddy.plugins.loader import load_mapping_plugin
        _mapping_plugin = load_mapping_plugin("default_mapping")
    
    try:
        return _mapping_plugin.classify(col_name)
    except Exception as e:
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


# ── AI_Mapper ───────────────────────────────────────────────────────────────
_ai_cache = {}

def is_ollama_running() -> bool:
    """Check if Ollama server is running at localhost:11434"""
    try:
        response = requests.get("http://localhost:11434", timeout=2)
        return response.status_code == 200
    except Exception:
        return False

def local_classify(col_name: str) -> str:
    """Classify a column name using local Ollama LLM."""
    # Check if Ollama is running
    if not is_ollama_running():
        logger.warning("Ollama not reachable at localhost:11434")
    
    prompt = (
        f"Classify the database column '{col_name}' into one word from: "
        "value, quantity, name, date, identifier, status, description. "
        "Return only one word."
    )

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3",
                "prompt": prompt,
                "stream": False
            },
            timeout=5
        )

        result = response.json().get("response", "").strip().lower()

        if result.isalnum():
            return result

    except Exception:
        logger.warning(f"Local classification failed for '{col_name}'")

    return "unknown"


def openai_classify(col_name: str) -> str:
    """Classify a column name using OpenAI API with caching."""
    # Check cache first
    if col_name in _ai_cache:
        return _ai_cache[col_name]
    
    api_key = os.getenv("OPENAI_API_KEY")
    
    prompt = (
        f"Classify the database column '{col_name}' into one word from: "
        "value, quantity, name, date, identifier, status, description. "
        "Return only one word."
    )

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 5,
                "temperature": 0
            },
            timeout=10
        )

        data = response.json()
        result = data["choices"][0]["message"]["content"].strip().lower()

        # Validate response (must be alphanumeric)
        if result.isalnum():
            _ai_cache[col_name] = result
            return result

    except Exception:
        pass

    _ai_cache[col_name] = "unknown"
    return "unknown"


def classify_column(col_name: str, provider: str) -> str:
    """Route classification to appropriate AI provider."""
    if provider == "local":
        return local_classify(col_name)
    elif provider == "openai":
        return openai_classify(col_name)
    elif provider == "hybrid":
        result = local_classify(col_name)
        if result == "unknown":
            return openai_classify(col_name)
        return result
    else:
        return "unknown"


def batch_local_classify(col_names: list[str]) -> dict[str, str]:
    """Classify multiple columns using local Ollama LLM in a single batch."""
    prompt = (
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with column names as keys.\n\n"
        f"Columns: {col_names}"
    )

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3",
                "prompt": prompt,
                "stream": False
            },
            timeout=8
        )

        text = response.json().get("response", "{}")

        import json
        parsed = json.loads(text)

        return {
            col: val.lower() if val and val.isalnum() else "unknown"
            for col, val in parsed.items()
            if col in col_names
        }

    except Exception:
        return {col: "unknown" for col in col_names}


def batch_openai_classify(col_names: list[str]) -> dict[str, str]:
    """Classify multiple columns using OpenAI API in a single batch."""
    api_key = os.getenv("OPENAI_API_KEY")

    prompt = (
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with column names as keys.\n\n"
        f"Columns: {col_names}"
    )

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100
            },
            timeout=10
        )

        text = response.json()["choices"][0]["message"]["content"]

        import json
        parsed = json.loads(text)

        return {
            col: val.lower() if val and val.isalnum() else "unknown"
            for col, val in parsed.items()
            if col in col_names
        }

    except Exception:
        return {col: "unknown" for col in col_names}


def batch_classify_columns(col_names: list[str], provider: str) -> dict[str, str]:
    """Route batch classification to appropriate AI provider."""
    if provider == "local":
        return batch_local_classify(col_names)
    elif provider == "openai":
        return batch_openai_classify(col_names)
    elif provider == "hybrid":
        results = batch_local_classify(col_names)

        # Fallback only for unknowns
        unknowns = [c for c, r in results.items() if r == "unknown"]

        if unknowns:
            fallback = batch_openai_classify(unknowns)
            results.update(fallback)

        return results
    else:
        return {col: "unknown" for col in col_names}


def ai_refine(semantic_layer: dict[str, dict[str, dict]], provider: str = "local") -> dict[str, dict[str, dict]]:
    """Refine semantic layer by replacing 'unknown' terms with AI classifications."""
    # Collect unknown columns
    unknown_cols = []
    for table, columns in semantic_layer.items():
        for col, value in columns.items():
            if value["term"] == "unknown":
                unknown_cols.append((table, col))

    if not unknown_cols:
        return semantic_layer

    col_names = [col for _, col in unknown_cols]
    
    logger.info(f"[{provider}] Batch classifying {len(col_names)} columns")
    print(f"[*] AI classifying {len(col_names)} unknown columns using {provider} provider...")

    # Batch classify all unknown columns
    results = batch_classify_columns(col_names, provider)
    
    logger.info(f"[{provider}] Batch classification completed")

    # Apply results back to semantic layer
    for table, col in unknown_cols:
        refined = results.get(col, "unknown")
        semantic_layer[table][col] = {
            "term": refined,
            "source": provider
        }

    return semantic_layer

# ── Output_Writer ───────────────────────────────────────────────────────────
def write_output(semantic_layer, output_path: str = "output.json") -> str:
    output_path = os.path.abspath(output_path)
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(semantic_layer, f, indent=2)
        os.replace(tmp_path, output_path)
    except Exception as e:
        logger.error(f"Output write failed: {str(e)}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return output_path

# ── Config Loader ───────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load configuration from JSON file if --config flag is provided."""
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            path = sys.argv[idx + 1]
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                    logger.info(f"Configuration loaded from {path}")
                    return config
            except FileNotFoundError:
                logger.error(f"Config file not found: {path}")
                print(f"[-] Config file not found: {path}")
                sys.exit(1)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in config file: {str(e)}")
                print(f"[-] Invalid JSON in config file: {e}")
                sys.exit(1)
            except Exception as e:
                logger.error(f"Failed to load config: {str(e)}")
                print(f"[-] Failed to load config: {e}")
                sys.exit(1)
    return {}


# ── CLI Entry Point ──────────────────────────────────────────────────────────
def main():
    # ── Version flag ───────────────────────────────────────────────────────────
    if "--version" in sys.argv:
        from dbbuddy import __version__
        print(f"dbbuddy v{__version__}")
        sys.exit(0)
    
    # ── Load configuration ──────────────────────────────────────────────────────
    config = load_config()
    
    # ── --ai flag check ──────────────────────────────────────────────────────
    use_ai = config.get("ai", False)
    if "--ai" in sys.argv:
        use_ai = True
        if config.get("ai", False):
            logger.info("CLI override: AI enabled")
    
    # ── AI provider selection ─────────────────────────────────────────────────
    provider = config.get("ai_provider", "local")  # default from config or "local"
    if "--ai-provider" in sys.argv:
        idx = sys.argv.index("--ai-provider")
        if idx + 1 < len(sys.argv):
            cli_provider = sys.argv[idx + 1]
            if config.get("ai_provider") and config.get("ai_provider") != cli_provider:
                logger.info(f"CLI override: AI provider changed from {config.get('ai_provider')} to {cli_provider}")
            provider = cli_provider
    
    # Validate provider
    valid_providers = ["local", "openai", "hybrid"]
    if provider not in valid_providers:
        logger.error(f"Invalid AI provider: {provider}")
        print(f"[-] Invalid AI provider: {provider}")
        print(f"[*] Valid providers: {', '.join(valid_providers)}")
        sys.exit(1)
    
    # Check OPENAI_API_KEY for providers that need it
    if use_ai and provider in ["openai", "hybrid"] and "OPENAI_API_KEY" not in os.environ:
        logger.error("OPENAI_API_KEY required for openai/hybrid provider")
        print("[-] OPENAI_API_KEY is required when using --ai with openai or hybrid provider")
        sys.exit(1)
    
    # Log configuration
    logger.info(f"AI enabled: {use_ai}, Provider: {provider}")
    
    # ── Load mapping plugin ─────────────────────────────────────────────────────
    from dbbuddy.plugins.loader import load_mapping_plugin
    plugin_name = config.get("mapping_plugin", "default_mapping")
    global _mapping_plugin
    _mapping_plugin = load_mapping_plugin(plugin_name)
    logger.info(f"Using mapping plugin: {plugin_name}")
    print(f"[*] Using mapping plugin: {plugin_name}")

    # ── Connection prompts ───────────────────────────────────────────────────
    host = config["host"] if "host" in config else (input("Host [localhost]: ").strip() or "localhost")

    username = config["user"] if "user" in config else ""
    while not username:
        username = input("Username: ").strip()
        if not username:
            print("[-] Username is required.")

    password = getpass.getpass("Password: ")

    database = config["database"] if "database" in config else ""
    while not database:
        database = input("Database: ").strip()
        if not database:
            print("[-] Database name is required.")

    # ── Connect ──────────────────────────────────────────────────────────────
    conn = connect_db(host, username, password, database)
    if conn is None:
        logger.error("Database connection failed")
        print("[-] Failed to connect to database.")
        sys.exit(1)
    
    logger.info("Database connection established")

    # ── Fetch schema ─────────────────────────────────────────────────────────
    schema = fetch_schema(conn)
    if schema is None:
        logger.error("Schema fetch failed")
        print("[-] Failed to fetch schema.")
        sys.exit(1)
    
    logger.info(f"Schema fetched: {len(schema)} tables")

    # ── Map schema ───────────────────────────────────────────────────────────
    logger.info("Generating semantic layer output")
    semantic_layer = map_schema(schema)
    
    # ── AI refinement (if --ai flag is set) ───────────────────────────────────
    if use_ai:
        logger.info(f"AI provider selected: {provider}")
        print(f"[*] Using AI to refine unknown columns (provider: {provider})...")
        semantic_layer = ai_refine(semantic_layer, provider)

    # ── Write output ─────────────────────────────────────────────────────────
    output_path = config["output"] if "output" in config else "output.json"
    try:
        output_path = write_output(semantic_layer, output_path)
    except (IOError, ValueError) as e:
        logger.error(f"Output write failed: {str(e)}")
        print(f"[-] Failed to write output: {e}")
        sys.exit(1)

    logger.info(f"Output written to {output_path}")
    print(f"[+] Output written to {output_path}")

if __name__ == "__main__":
    main()
