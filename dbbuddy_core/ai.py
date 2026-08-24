# ── AI_Mapper ───────────────────────────────────────────────────────────────
import os
import re
import requests
import logging

from dbbuddy_core import secrets_store

logger = logging.getLogger(__name__)

# The only supported AI providers:
#   local    -> Qwen Coder via Ollama (offline semantic labeling)
#   nemotron -> NVIDIA Nemotron cloud API
#   openai   -> OpenAI cloud API
#   hybrid   -> Qwen for labeling with Nemotron in the intelligence layer
VALID_PROVIDERS = ("local", "nemotron", "openai", "hybrid")

# Nemotron model id. Configurable so a different model can be used without code
# changes — defaults to the model behind build.nvidia.com/nvidia/<this slug>.
DEFAULT_NEMOTRON_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

# OpenAI model id. Configurable via OPENAI_MODEL.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Local (Ollama) tuning. Cold-loading a 7B model — especially during GPU init or
# under VRAM pressure — can take noticeably longer than a warm call, so the
# request timeout is generous and configurable. keep_alive keeps the model
# resident between analyses to avoid repeated cold starts.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

# Ollama is local — never route it through an HTTP(S) proxy. A proxy inherited
# from the environment (common on VPN/corporate setups) is a frequent cause of
# the localhost call failing only when the server runs in a different shell.
_OLLAMA_NO_PROXY = {"http": None, "https": None}

# Most recent AI provider error, surfaced so a silent fallback to rule-based
# labeling can be explained (which provider failed and why).
_last_provider_error = None


def get_last_provider_error():
    """Return the most recent AI provider error message, or None."""
    return _last_provider_error


def _clear_provider_error():
    global _last_provider_error
    _last_provider_error = None


def _record_provider_error(message: str) -> None:
    global _last_provider_error
    _last_provider_error = message
    logger.warning(message)


def _extract_json_object(text) -> str:
    """Best-effort isolation of a JSON object from a model response.

    Tolerant of the common ways models wrap JSON, so switching providers doesn't
    require new parsing logic:
      * reasoning blocks      —  <think> … </think>
      * markdown code fences  —  ```json … ```  or  ``` … ```
      * explanatory prose     —  "Here's the JSON: { … }"

    Returns the best-effort ``{ … }`` substring (or the input if none is found).
    """
    if not isinstance(text, str):
        return "{}"

    # 1. Drop reasoning blocks emitted before the answer (e.g. Nemotron).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 2. Prefer the contents of a fenced code block when one wraps the JSON.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence and "{" in fence.group(1):
        text = fence.group(1)

    # 3. Narrow to the outermost { … } span — tolerant of surrounding prose.
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def _describe_request_error(provider: str, exc: Exception, response=None) -> str:
    """Build a human-readable reason for an AI request failure."""
    if response is not None:
        try:
            body = (response.text or "").strip().replace("\n", " ")
            return f"{provider} API returned HTTP {response.status_code}: {body[:300]}"
        except Exception:
            pass
    return f"{provider} request failed: {type(exc).__name__}: {exc}"


_TOKEN_EXPANSIONS = {
    "id":   "identifier",
    "ids":  "identifiers",
    "qty":  "quantity",
    "amt":  "amount",
    "val":  "value",
    "num":  "number",
    "desc": "description",
    "ts":   "timestamp",
    "dt":   "date",
    "url":  "url",
    "img":  "image",
    "msg":  "message",
    "ref":  "reference",
    "pct":  "percent",
}


def _normalize(col_name: str) -> str:
    """Normalize a column name into a human-readable semantic fallback term.

    Pipeline:
      1. Split camelCase / PascalCase  (productName → 'product Name')
      2. Replace underscores with spaces (created_at → 'created at')
      3. Lowercase
      4. Expand common abbreviations   (user id → 'user identifier')

    Examples:
      created_at  → 'created at'
      productName → 'product name'
      orderID     → 'order identifier'
      userAmt     → 'user amount'
    """
    col = re.sub(r'([a-z])([A-Z])', r'\1 \2', col_name)  # camelCase split
    col = col.replace("_", " ").lower()
    tokens = [_TOKEN_EXPANSIONS.get(t, t) for t in col.split()]
    return " ".join(tokens)


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

def _fallback_result(col_name: str) -> dict:
    """Rule-based result used when no AI provider produced a term.

    provider=None marks this as a non-AI fallback so callers can label the
    semantic layer honestly (source "rule", not "ai").
    """
    return {"term": _normalize(col_name), "provider": None}


CANONICAL_TERMS = ("value", "quantity", "name", "date", "identifier", "status", "description")


def _as_result(value, col_name: str, provider: str) -> dict:
    """Wrap a raw model value into a provenance result.

    Accepts a clean single-word answer, and also tolerates a slightly off answer
    (extra words/punctuation, e.g. "user identifier") by extracting the first
    canonical term it contains — so a usable local answer isn't discarded and
    needlessly escalated to the cloud provider.
    """
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned and cleaned != "unknown":
            if cleaned.isalnum():
                return {"term": cleaned, "provider": provider}
            for term in CANONICAL_TERMS:
                if term in cleaned:
                    return {"term": term, "provider": provider}
    return _fallback_result(col_name)


def _match_value(parsed, col_key: str):
    """Look up a column's classification tolerantly.

    The model is asked for keys like "users.id" but at times returns the bare
    column ("id") or different casing. Try exact, then case-insensitive, then the
    bare column name — so a valid answer isn't dropped over key formatting.
    """
    if not isinstance(parsed, dict):
        return None
    if col_key in parsed:
        return parsed[col_key]
    bare = col_key.split(".")[-1]
    for key, value in parsed.items():
        if not isinstance(key, str):
            continue
        if key.lower() == col_key.lower():
            return value
        if key.split(".")[-1].lower() == bare.lower():
            return value
    return None


def _unpack_result(raw, requested_provider: str):
    """Normalize a classification result into a (term, provider) pair.

    Accepts the provenance dict produced by the batch classifiers, or a plain
    string (from a simple caller or a test mock) which is treated as an AI
    result from the requested provider.
    """
    if isinstance(raw, dict):
        return raw.get("term"), raw.get("provider")
    if isinstance(raw, str):
        return raw, requested_provider
    return None, None


def is_ollama_running() -> bool:
    """Check if the Ollama server is reachable."""
    try:
        response = requests.get(OLLAMA_URL, timeout=5, proxies=_OLLAMA_NO_PROXY)
        return response.status_code == 200
    except Exception:
        return False

def local_classify(col_name: str) -> str:
    """Classify a column name using local Ollama LLM."""
    logger = logging.getLogger(__name__)
    # Check if Ollama is running
    if not is_ollama_running():
        logger.warning("Ollama not reachable at 127.0.0.1:11434")

    # Use Qwen Coder as the primary local model
    local_model = os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b")

    prompt = (
        f"Classify the database column '{col_name}' into one word from: "
        "value, quantity, name, date, identifier, status, description. "
        "Return only one word."
    )

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": local_model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
            },
            timeout=OLLAMA_TIMEOUT,
            proxies=_OLLAMA_NO_PROXY,
        )

        result = response.json().get("response", "").strip().lower()

        if result.isalnum():
            return result

    except Exception:
        logger.warning(f"Local classification failed for '{col_name}'")

    return _normalize(col_name)  # fallback to column name, never "unknown"


def nemotron_classify(col_name: str) -> str:
    """Classify a column name using the Nemotron cloud API."""
    nemotron_api_key = secrets_store.get_api_key("nemotron")
    nemotron_endpoint = os.getenv("NEMOTRON_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")
    nemotron_model = os.getenv("NEMOTRON_MODEL", DEFAULT_NEMOTRON_MODEL)

    if not nemotron_api_key:
        _record_provider_error("Nemotron API key is not configured.")
        return _normalize(col_name)

    prompt = (
        f"Classify the database column '{col_name}' into one word from: "
        "value, quantity, name, date, identifier, status, description. "
        "Return only one word."
    )

    response = None
    try:
        response = requests.post(
            nemotron_endpoint,
            headers={
                "Authorization": f"Bearer {nemotron_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": nemotron_model,
                "messages": [
                    {"role": "system", "content": "You classify database columns. Return only one word."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 5,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip().lower()

        if result.isalnum():
            return result

    except Exception as exc:
        _record_provider_error(_describe_request_error("Nemotron", exc, response))

    return _normalize(col_name)


def openai_classify(col_name: str) -> str:
    """Single-column classification via the OpenAI batch path."""
    result = batch_openai_classify([col_name]).get(col_name)
    term, _ = _unpack_result(result, "openai")
    return term or _normalize(col_name)


def classify_column(col_name: str, provider: str) -> str:
    """Route classification to appropriate AI provider."""
    if provider == "local":
        return local_classify(col_name)
    elif provider == "nemotron":
        return nemotron_classify(col_name)
    elif provider == "openai":
        return openai_classify(col_name)
    elif provider == "hybrid":
        result = local_classify(col_name)
        if result == "unknown":
            return nemotron_classify(col_name)
        return result
    else:
        return _normalize(col_name)  # fallback to column name


def _local_classify_chunk(col_names: list[str], knowledge_base, local_model: str) -> dict[str, dict]:
    """One Ollama request for a bounded set of columns."""
    prompt = (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
        f"Columns: {col_names}"
    )
    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": local_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",            # force valid JSON output
            # Ollama defaults num_ctx to 2048 regardless of the model's real
            # window; a labeling prompt + its JSON reply overflows that and comes
            # back truncated (invalid JSON). Give it the model's capacity.
            "options": {
                "temperature": 0,        # deterministic
                "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
                "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "4096")),
            },
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
        timeout=OLLAMA_TIMEOUT,
        proxies=_OLLAMA_NO_PROXY,
    )
    import json
    text = response.json().get("response", "{}")
    parsed = json.loads(_extract_json_object(text))
    return {col: _as_result(_match_value(parsed, col), col, "local") for col in col_names}


def batch_local_classify(col_names: list[str], schema: dict | None = None) -> dict[str, dict]:
    """Classify multiple columns using local Ollama LLM.

    Returns {col: {"term": str, "provider": "local" | None}}. provider is
    "local" when the model produced the term and None when it fell back to the
    rule-based normalized name (e.g. Ollama not running).

    Classified in bounded chunks: one request for every column produces a single
    JSON object whose size grows with the schema width, and on a wide schema (e.g.
    AdventureWorks' 465 columns) that reply overflows the model's context and
    returns truncated — invalid JSON — so *every* column silently drops to
    rule-based. Chunking keeps each reply parseable; a chunk that still fails
    degrades only its own columns.
    """
    knowledge_base = build_knowledge_base(schema)
    local_model = os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b")
    chunk_size = int(os.getenv("LOCAL_CLASSIFY_CHUNK", "30"))

    results: dict[str, dict] = {}
    for start in range(0, len(col_names), chunk_size):
        chunk = col_names[start:start + chunk_size]
        try:
            results.update(_local_classify_chunk(chunk, knowledge_base, local_model))
        except Exception as exc:  # noqa: BLE001
            _record_provider_error(_describe_request_error("Local (Ollama)", exc, None))
            results.update({col: _fallback_result(col) for col in chunk})  # rule-based fallback
    return results


def batch_openai_classify(col_names: list[str], schema: dict | None = None) -> dict[str, dict]:
    """Classify multiple columns using the OpenAI API in a single batch.

    Returns {col: {"term": str, "provider": "openai" | None}}. provider is None
    (rule-based fallback) when the API key is missing or the call fails. Mirrors
    the Nemotron path so the two cloud providers behave identically.
    """
    openai_api_key = secrets_store.get_api_key("openai")
    openai_endpoint = os.getenv("OPENAI_ENDPOINT", "https://api.openai.com/v1/chat/completions")
    openai_model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    knowledge_base = build_knowledge_base(schema)

    if not openai_api_key:
        _record_provider_error("OpenAI API key is not configured.")
        return {col: _fallback_result(col) for col in col_names}

    prompt = (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
        f"Columns: {col_names}"
    )

    response = None
    try:
        response = requests.post(
            openai_endpoint,
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": openai_model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},  # force valid JSON
                "max_tokens": 1024,
                "temperature": 0,
            },
            timeout=60,
        )
        response.raise_for_status()

        text = response.json()["choices"][0]["message"]["content"]

        import json
        parsed = json.loads(_extract_json_object(text))

        return {col: _as_result(_match_value(parsed, col), col, "openai") for col in col_names}

    except Exception as exc:
        _record_provider_error(_describe_request_error("OpenAI", exc, response))
        return {col: _fallback_result(col) for col in col_names}  # rule-based fallback


def batch_nemotron_classify(col_names: list[str], schema: dict | None = None) -> dict[str, dict]:
    """Classify multiple columns using Nemotron 3 Ultra API in a single batch.

    Returns {col: {"term": str, "provider": "nemotron" | None}}. provider is
    None (rule-based fallback) when the API key is missing or the call fails.
    """
    nemotron_api_key = secrets_store.get_api_key("nemotron")
    nemotron_endpoint = os.getenv("NEMOTRON_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")
    nemotron_model = os.getenv("NEMOTRON_MODEL", DEFAULT_NEMOTRON_MODEL)
    knowledge_base = build_knowledge_base(schema)

    if not nemotron_api_key:
        _record_provider_error("Nemotron API key is not configured.")
        return {col: _fallback_result(col) for col in col_names}

    prompt = (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
        f"Columns: {col_names}"
    )

    response = None
    try:
        response = requests.post(
            nemotron_endpoint,
            headers={
                "Authorization": f"Bearer {nemotron_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": nemotron_model,
                # Nemotron is a reasoning model: "detailed thinking off" makes it
                # answer directly instead of spending the token budget on a
                # <think> block (which previously left no room for the JSON).
                "messages": [
                    {"role": "system", "content": "detailed thinking off"},
                    {"role": "user", "content": prompt},
                ],
                # Generous budget so the full JSON mapping is never truncated.
                "max_tokens": 1024,
                "temperature": 0
            },
            timeout=60
        )
        response.raise_for_status()

        text = response.json()["choices"][0]["message"]["content"]

        import json
        parsed = json.loads(_extract_json_object(text))

        return {col: _as_result(_match_value(parsed, col), col, "nemotron") for col in col_names}

    except Exception as exc:
        _record_provider_error(_describe_request_error("Nemotron", exc, response))
        return {col: _fallback_result(col) for col in col_names}  # rule-based fallback


def batch_classify_columns(col_names: list[str], provider: str, schema: dict | None = None) -> dict[str, dict]:
    """Route batch classification to the appropriate AI provider.

    Returns {col: {"term": str, "provider": str | None}}.
    """
    if provider == "local":
        return batch_local_classify(col_names, schema)
    elif provider == "nemotron":
        return batch_nemotron_classify(col_names, schema)
    elif provider == "openai":
        return batch_openai_classify(col_names, schema)
    elif provider == "hybrid":
        results = batch_local_classify(col_names, schema)

        # Fall back to Nemotron for any column the local model could not classify
        # (provider is None means it fell back to the rule-based name).
        unresolved = [
            c for c, r in results.items()
            if not (isinstance(r, dict) and r.get("provider"))
        ]

        if unresolved:
            fallback = batch_nemotron_classify(unresolved, schema)
            results.update(fallback)

        return results
    else:
        return {col: _fallback_result(col) for col in col_names}  # rule-based fallback


def ai_refine(
    semantic_layer: dict[str, dict[str, dict]],
    provider: str = "local",
    schema: dict | None = None,
    provider_chain=None,
) -> dict[str, dict[str, dict]]:
    """Refine semantic layer by reclassifying schema terms with AI using DB context.

    ``provider_chain`` (a list of ``ProviderRuntimeConfig``) is the provider-agnostic
    path: when supplied, classification is routed through the adapter registry
    (``ai_providers.classify_columns``), which also handles fallback. When it is
    ``None``, the legacy ``provider``-string path is used unchanged (Ollama / Nemotron
    / OpenAI / hybrid) so existing callers and the CLI keep working.
    """
    logger = logging.getLogger(__name__)

    # Legacy string path only validates the fixed provider set. The registry path
    # accepts arbitrary configured adapters, so it skips this check.
    if provider_chain is None and provider not in VALID_PROVIDERS:
        raise ValueError(
            f"Unsupported AI provider '{provider}'. Supported: {', '.join(VALID_PROVIDERS)}."
        )

    # Reset provider-error state so any error reflects only this run.
    _clear_provider_error()

    column_keys = [f"{table}.{col}" for table, columns in semantic_layer.items() for col in columns]
    if not column_keys:
        return semantic_layer

    if provider_chain is not None:
        from dbbuddy_core.ai_providers import classify_columns
        label = provider_chain[0].name if provider_chain else "chain"
        logger.info(f"[{label}] Classifying {len(column_keys)} columns via provider chain")
        results = classify_columns(column_keys, schema, provider_chain)
    else:
        logger.info(f"[{provider}] Batch classifying {len(column_keys)} columns from schema context")
        logger.debug(f"[*] AI classifying {len(column_keys)} columns using {provider} provider...")
        results = batch_classify_columns(column_keys, provider, schema=schema)

    logger.info(f"[{provider}] Batch classification completed")

    for table, columns in semantic_layer.items():
        for col in columns:
            key = f"{table}.{col}"
            raw = results.get(col)
            if raw is None:
                raw = results.get(key)

            term, actual_provider = _unpack_result(raw, provider)

            # Strip accidental table prefix (e.g. "events.id" → "id")
            if term and "." in term:
                term = term.split(".")[-1]

            # Never store empty or "unknown" — fall back to the normalized column
            # name and record it as a rule-based result, not an AI one.
            if not term or term == "unknown":
                term = _normalize(col)
                actual_provider = None

            semantic_layer[table][col] = {
                "term": term,
                # Honest provenance: only "ai" when a provider actually answered.
                "source": "ai" if actual_provider else "rule",
                "provider": actual_provider,
            }

    return semantic_layer
