# ── AI_Mapper ───────────────────────────────────────────────────────────────
import os
import requests
import logging

_ai_cache = {}


def build_knowledge_base(schema: dict) -> dict:
    """Build a compact schema summary for AI prompts."""
    if not schema:
        return {"tables": []}

    return {
        "tables": {
            table: [col for col in columns]
            for table, columns in schema.items()
        }
    }

def is_ollama_running() -> bool:
    """Check if Ollama server is running at localhost:11434"""
    try:
        response = requests.get("http://localhost:11434", timeout=2)
        return response.status_code == 200
    except Exception:
        return False

def local_classify(col_name: str) -> str:
    """Classify a column name using local Ollama LLM."""
    logger = logging.getLogger(__name__)
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
    logger = logging.getLogger(__name__)
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


def batch_local_classify(col_names: list[str], schema: dict | None = None) -> dict[str, str]:
    """Classify multiple columns using local Ollama LLM in a single batch."""
    logger = logging.getLogger(__name__)
    knowledge_base = build_knowledge_base(schema)
    prompt = (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
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


def batch_openai_classify(col_names: list[str], schema: dict | None = None) -> dict[str, str]:
    """Classify multiple columns using OpenAI API in a single batch."""
    logger = logging.getLogger(__name__)
    api_key = os.getenv("OPENAI_API_KEY")
    knowledge_base = build_knowledge_base(schema)

    prompt = (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
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


def batch_classify_columns(col_names: list[str], provider: str, schema: dict | None = None) -> dict[str, str]:
    """Route batch classification to appropriate AI provider."""
    if provider == "local":
        return batch_local_classify(col_names, schema)
    elif provider == "openai":
        return batch_openai_classify(col_names, schema)
    elif provider == "hybrid":
        results = batch_local_classify(col_names, schema)

        # Fallback only for unknowns
        unknowns = [c for c, r in results.items() if r == "unknown"]

        if unknowns:
            fallback = batch_openai_classify(unknowns, schema)
            results.update(fallback)

        return results
    else:
        return {col: "unknown" for col in col_names}


def ai_refine(semantic_layer: dict[str, dict[str, dict]], provider: str = "local", schema: dict | None = None) -> dict[str, dict[str, dict]]:
    """Refine semantic layer by reclassifying schema terms with AI using DB context."""
    logger = logging.getLogger(__name__)

    column_keys = [f"{table}.{col}" for table, columns in semantic_layer.items() for col in columns]
    if not column_keys:
        return semantic_layer

    logger.info(f"[{provider}] Batch classifying {len(column_keys)} columns from schema context")
    print(f"[*] AI classifying {len(column_keys)} columns using {provider} provider...")

    results = batch_classify_columns(column_keys, provider, schema=schema)

    logger.info(f"[{provider}] Batch classification completed")

    for table, columns in semantic_layer.items():
        for col in columns:
            key = f"{table}.{col}"
            refined = results.get(key, results.get(col, "unknown"))
            semantic_layer[table][col] = {
                "term": refined,
                "source": "ai",
                "provider": provider,
            }

    return semantic_layer
