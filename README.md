# DB Buddy

An AI-powered semantic database interface. Ask questions in plain English, get SQL grounded in your actual schema, and receive results with the reasoning behind them.

## Key highlights

- 🧠 **Semantic-layer grounded SQL** — every query is built against a structured understanding of your schema, not raw column names
- 🛡️ **Safe execution with approval flow** — SELECT runs automatically; anything that mutates data waits for explicit sign-off
- 🔁 **Self-healing query pipeline** — failed queries are repaired by AI and retried, with confidence scoring on the result
- ⚡ **Hybrid AI support** — runs fully offline via local Ollama; falls back to OpenAI only when local is unavailable

---

## Design principles

- **Separation of concerns** — core logic (`dbbuddy_core`) is completely independent of its interfaces (CLI, API, UI). Each layer can be replaced or extended without touching the others.
- **Safety-first execution** — SELECT queries run automatically; anything that mutates data requires explicit approval. The system never bypasses this boundary.
- **Semantic grounding for AI reliability** — SQL is generated against a structured semantic layer, not raw column names. This reduces hallucination and improves query correctness.
- **Pluggable architecture** — the mapping system is a swappable plugin. Drop in a custom classifier without changing any core code.
- **Multi-provider AI** — local Ollama runs fully offline with no API key. OpenAI is an optional upgrade. Hybrid mode uses local first and only reaches the cloud when local fails.

---

## What makes it different

Most NL→SQL tools translate your question blindly. DB Buddy first builds a **semantic layer** — a map of what every column in your database actually means (value, identifier, date, status, etc.) — and grounds every SQL query in that understanding.

| Feature | Typical tool | DB Buddy |
|---|---|---|
| NL → SQL | Basic translation | Semantic-grounded |
| Safety | None | Controlled execution with approval path |
| Error handling | Fail and stop | Auto-fix loop with retry |
| AI provider | Single (cloud only) | Multi-provider: OpenAI + Ollama |
| Local LLM | ❌ | ✅ (Ollama, runs fully offline) |
| Architecture | Script | Modular: `dbbuddy_core` + CLI + API + Frontend |

---

## Features

- **Semantic schema understanding** — classifies every column into terms like `value`, `quantity`, `name`, `date`, `identifier`, `status`, `description`
- **Natural language → SQL** — generates SQL grounded in the semantic layer
- **Safe execution** — SELECT queries auto-run; INSERT/UPDATE/DELETE stay in a review/approval path
- **Auto-fix loop** — if a SELECT fails, DB Buddy repairs the SQL and retries automatically
- **Confidence scoring** — `high` (clean run), `medium` (auto-fixed or non-SELECT), `low` (fix also failed)
- **Hybrid AI** — tries local Ollama first; falls back to OpenAI only if local is unavailable
- **Plugin system** — swap out the mapping logic without touching core code
- **Full-stack** — FastAPI backend + React frontend + CLI, all sharing `dbbuddy_core`

---

## Architecture

### Module map

```
dbbuddy_core/     ← Core engine (pipeline, mapping, AI, query, schema, DB)
  └── plugins/    ← Pluggable mapping system
dbbuddy/          ← CLI entry point (thin: input/output only)
backend/          ← FastAPI REST API
frontend/         ← React + TanStack Start UI
tests/            ← 96 tests including property-based (Hypothesis)
```

> **Note on naming:** `dbbuddy_core` and `dbbuddy` use underscores because they are Python packages — hyphens are not valid in Python import paths. The logical roles are: `dbbuddy_core` = core library, `dbbuddy` = CLI wrapper.

### Data flow

```
 User (plain English)
        │
        ▼
 ┌─────────────┐     ┌──────────────┐
 │  CLI / API  │────▶│  dbbuddy_core │
 └─────────────┘     └──────┬───────┘
                             │
              ┌──────────────▼──────────────┐
              │  1. Fetch schema             │  SHOW TABLES + DESCRIBE
              │  2. Build semantic layer     │  classify every column
              │  3. Generate SQL             │  AI prompt + semantic map
              │  4. Validate query           │  type check + syntax guard
              │  5. Execute (safe path)      │  SELECT auto / mutating → hold
              │  6. Auto-fix if failed       │  repair SQL, retry, score
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  AI provider routing         │
              │  hybrid: local → OpenAI      │
              │  local:  Ollama (offline)    │
              │  openai: gpt-4o-mini         │
              └─────────────────────────────┘
```

---

## Quick start

**Prerequisites:** Python 3.10+, MySQL, [Ollama](https://ollama.com) (optional, for local AI)

```bash
# 1. Clone and set up
git clone https://github.com/sxjalxo/dbbuddy.git
cd dbbuddy
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/Mac

# 2. Install
pip install -r requirements.txt
pip install -e .

# 3. Run the CLI
dbbuddy

# Or with AI
dbbuddy --ai                              # local Ollama (default)
dbbuddy --ai --ai-provider openai        # OpenAI (needs OPENAI_API_KEY)
dbbuddy --ai --ai-provider hybrid        # local first, OpenAI fallback
```

You'll be prompted for host, username, password, and database name. Password is always collected via `getpass` — never stored.

### Config file mode

```bash
dbbuddy --config config.json
```

```json
{
  "host": "localhost",
  "user": "root",
  "database": "mydb",
  "ai": true,
  "ai_provider": "hybrid"
}
```

---

## AI providers

| Provider | Model | When to use |
|---|---|---|
| `local` | deepseek-coder via Ollama | Default. Runs fully offline, no API key needed |
| `openai` | gpt-4o-mini | Cloud. Requires `OPENAI_API_KEY` |
| `hybrid` | local → OpenAI | Local first; falls back to OpenAI only if Ollama is unreachable or returns invalid SQL |

**Hybrid priority rule:** DB Buddy always tries the local Ollama model first in hybrid mode. It only falls back to the OpenAI API if the Ollama server is unreachable, times out, or returns an unusable result.

### Local AI setup (Ollama)

```bash
ollama pull deepseek-coder
ollama serve          # starts server at http://127.0.0.1:11434
```

---

## How it works internally

Every query goes through a fixed six-step pipeline inside `dbbuddy_core`:

```
1. Fetch schema        →  SHOW TABLES + DESCRIBE for every table
2. Build semantic layer →  classify each column: value / identifier / date / ...
3. Generate SQL        →  AI prompt grounded in the semantic layer
4. Validate query      →  type check (SELECT vs mutating), basic syntax guard
5. Execute safely      →  SELECT auto-runs; INSERT/UPDATE/DELETE go to approval path
6. Auto-fix if failed  →  repair SQL with AI, retry once, score confidence
```

The CLI and backend are thin wrappers — all intelligence lives in `dbbuddy_core/pipeline.py`.

---

## API reference

```bash
cd backend
uvicorn main:app --reload   # starts at http://127.0.0.1:8000
```

### `POST /query` — natural language to SQL + results

```json
{
  "host": "localhost",
  "user": "root",
  "password": "secret",
  "database": "mydb",
  "question": "total revenue last month",
  "ai": true,
  "ai_provider": "hybrid"
}
```

Response:

```json
{
  "query": "total revenue last month",
  "sql": "SELECT SUM(amount) AS revenue FROM orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 1 MONTH)",
  "query_type": "select",
  "auto_executed": true,
  "auto_fixed": false,
  "confidence": "high",
  "results": [{ "revenue": 48320.50 }],
  "semantic_layer": { "orders": { "amount": { "term": "value", "source": "rule" } } }
}
```

### `POST /analyze` — semantic schema analysis only

```json
{ "host": "localhost", "user": "root", "password": "secret", "database": "mydb", "ai": false }
```

### `POST /execute` — run raw SQL

```json
{ "host": "localhost", "user": "root", "password": "secret", "database": "mydb", "sql": "SELECT COUNT(*) FROM users" }
```

---

## Frontend

```bash
cd frontend
bun install
bun run dev
```

The UI connects to `http://127.0.0.1:8000` by default.

---

## Testing

```bash
pytest tests/ -v
```

96 tests pass, including property-based tests via [Hypothesis](https://hypothesis.works):

- DB connection success/failure/exception paths
- Schema fetching (DESCRIBE called exactly once per table, completeness, exception handling)
- Semantic mapping (exact match, substring match, case-insensitive, longest keyword wins)
- Output writing (atomic temp-file pattern, indentation, overwrite, error propagation)
- CLI behavior (re-prompts, defaults, exit codes)
- AI integration (success, timeout, caching, batch processing, provider routing)
- Plugin loader (valid plugin, invalid fallback, classify interface)

---

## Semantic mapping

The default plugin maps 27 keywords to 7 semantic terms:

| Keywords | Term |
|---|---|
| amt, amount, price, cost, total, revenue | `value` |
| qty, quantity, count, num, number | `quantity` |
| name, title, label | `name` |
| date, time, created_at, updated_at, timestamp | `date` |
| id, uuid, key | `identifier` |
| status, state, flag | `status` |
| desc, description, note, comment | `description` |

Matching: exact (case-insensitive) → substring (longest key wins) → normalized column name

### Custom plugins

```python
# dbbuddy_core/plugins/my_plugin.py
from dbbuddy_core.plugins.base import MappingPlugin

class Plugin(MappingPlugin):
    def classify(self, column_name: str) -> str:
        ...
```

```json
{ "mapping_plugin": "my_plugin" }
```

---

## Output format

```json
{
  "users": {
    "id":         { "term": "identifier", "source": "rule",   "plugin": "Plugin" },
    "email":      { "term": "contact",    "source": "openai", "plugin": "Plugin" },
    "created_at": { "term": "date",       "source": "rule",   "plugin": "Plugin" }
  }
}
```

Each column includes `term` (semantic classification), `source` (`"rule"` or the AI provider name), and `plugin` (the plugin class that ran).

---

## License

MIT — see [LICENSE](LICENSE)
