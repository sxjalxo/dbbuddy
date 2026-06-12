# DB Buddy

**A schema-aware AI database interface that doesn't just generate SQL — it validates, explains, and safely executes it.**

## Key highlights

- 🧠 **Semantic-layer grounded SQL** — every query is built against a structured understanding of your schema, not raw column names
- 🛡️ **Safe execution with approval flow** — SELECT runs automatically; anything that mutates data waits for explicit sign-off
- 🔁 **Self-healing query pipeline** — failed queries are repaired by AI and retried, with confidence scoring on the result
- ⚡ **Hybrid AI support** — runs fully offline via local models (Qwen/DeepSeek); falls back to Nemotron for complex queries

---

## Design principles

- **Separation of concerns** — core logic (`dbbuddy_core`) is completely independent of its interfaces (CLI, API, UI). Each layer can be replaced or extended without touching the others.
- **Safety-first execution** — SELECT queries run automatically; anything that mutates data requires explicit approval. The system never bypasses this boundary.
- **Semantic grounding for AI reliability** — SQL is generated against a structured semantic layer, not raw column names. This reduces hallucination and improves query correctness.
- **Pluggable architecture** — the mapping system is a swappable plugin. Drop in a custom classifier without changing any core code.
- **Multi-provider AI** — local Qwen/DeepSeek runs fully offline with no API key. Nemotron is an optional upgrade. Hybrid mode uses local first and only reaches the cloud when local fails.

---

## What makes it different

Most NL→SQL tools translate your question blindly. DB Buddy first builds a **semantic layer** — a map of what every column in your database actually means (value, identifier, date, status, etc.) — and grounds every SQL query in that understanding.

### 🚀 Key Differentiators

- **Schema-aware reasoning** — Not guess-based. Understands relationships between tables through foreign key inference
- **Deterministic SQL validation** — Every query is validated against your actual schema before execution
- **Multi-model fallback** — Local-first AI (Qwen Coder) with Nemotron fallback for reliability
- **Controlled execution** — No blind writes. READ queries auto-execute, WRITE queries require confirmation
- **Dry-run previews** — Shows estimated row counts and affected columns before destructive operations
- **Execution diff awareness** — Shows affected columns for UPDATE queries
- **Explainable AI** — Provides intent explanation, join reasoning, and confidence scoring
- **Observability dashboard** — Tracks execution patterns, fallback usage, and confidence distribution

| Feature | Typical tool | DB Buddy |
|---|---|---|
| NL → SQL | Basic translation | Semantic-grounded with relationship inference |
| Safety | None | Controlled execution with approval path |
| Error handling | Fail and stop | Auto-fix loop with retry |
| AI provider | Single (cloud only) | Multi-provider: Local + Nemotron |
| Local LLM | ❌ | ✅ (Qwen Coder, runs fully offline) |
| Dry-run preview | ❌ | ✅ (Row counts + affected columns) |
| Explainability | Basic SQL only | Intent + joins + confidence + reasoning |
| Observability | None | Metrics dashboard + query history |
| Architecture | Script | Modular: `dbbuddy_core` + CLI + API + Frontend |

---

## Features

- **Semantic schema understanding** — classifies every column into terms like `value`, `quantity`, `name`, `date`, `identifier`, `status`, `description`
- **Natural language → SQL** — generates SQL grounded in the semantic layer
- **Safe execution** — SELECT queries auto-run; INSERT/UPDATE/DELETE stay in a review/approval path
- **Auto-fix loop** — if a SELECT fails, DB Buddy repairs the SQL and retries automatically
- **Confidence scoring** — `high` (clean run), `medium` (auto-fixed or non-SELECT), `low` (fix also failed)
- **Hybrid AI** — tries local Qwen Coder first; falls back to Nemotron only if local is unavailable
- **Plugin system** — swap out the mapping logic without touching core code
- **Full-stack** — FastAPI backend + React frontend + CLI, all sharing `dbbuddy_core`
- **Dry-run preview** — Shows estimated impact before destructive operations
- **Join reasoning** — Explains which relationships were inferred for multi-table queries
- **Observability** — Tracks metrics, query history, and execution patterns

---

## Architecture

### Module map

```
dbbuddy_core/     ← Core engine (pipeline, mapping, AI, query, schema, DB)
  └── plugins/    ← Pluggable mapping system
dbbuddy/          ← CLI entry point (thin: input/output only)
backend/          ← FastAPI REST API
frontend/         ← React + TanStack Start UI
tests/            ← 237+ tests including property-based (Hypothesis), adversarial security tests, fuzz testing, chaos testing
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

## Demo

### Simple Query

**User:** List all users

**SQL:**
```sql
SELECT * FROM users;
```

**Response:**
```json
{
  "query": "List all users",
  "sql": "SELECT * FROM users;",
  "query_type": "select",
  "confidence": "high",
  "model_used": "local",
  "auto_executed": true,
  "results": [
    {"id": 1, "name": "Alice", "email": "alice@example.com"},
    {"id": 2, "name": "Bob", "email": "bob@example.com"}
  ]
}
```

### Complex Query with Joins

**User:** Top customers by spending

**SQL:**
```sql
SELECT u.name, SUM(o.total_amount) as total_spent
FROM users u
JOIN orders o ON u.id = o.user_id
GROUP BY u.id, u.name
ORDER BY total_spent DESC
LIMIT 10;
```

**Response:**
```json
{
  "query": "Top customers by spending",
  "sql": "SELECT u.name, SUM(o.total_amount) as total_spent FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id, u.name ORDER BY total_spent DESC LIMIT 10;",
  "query_type": "select",
  "confidence": "high",
  "model_used": "local",
  "join_reasoning": [
    {"relationship": "orders.user_id → users.id", "type": "foreign_key", "inferred": true}
  ],
  "auto_executed": true,
  "results": [
    {"name": "Alice", "total_spent": 15000.00},
    {"name": "Bob", "total_spent": 12500.00}
  ]
}
```

### Destructive Query with Safety

**User:** Delete user with id 5

**SQL:**
```sql
DELETE FROM users WHERE id = 5;
```

**Response:**
```json
{
  "query": "Delete user with id 5",
  "sql": "DELETE FROM users WHERE id = 5;",
  "query_type": "delete",
  "confidence": "high",
  "model_used": "local",
  "requires_confirmation": true,
  "auto_executed": false,
  "warning": "⚠️ This query will DELETE rows from 'users'. This action cannot be undone.\n⚠️ This will affect 1 row.",
  "dry_run": {
    "count_query": "SELECT COUNT(*) FROM users WHERE id = 5",
    "estimated_rows": 1
  }
}
```

### Update with Execution Diff Awareness

**User:** Update user name and email

**SQL:**
```sql
UPDATE users SET name = 'John', email = 'john@example.com' WHERE id = 10;
```

**Response:**
```json
{
  "query": "Update user name and email",
  "sql": "UPDATE users SET name = 'John', email = 'john@example.com' WHERE id = 10;",
  "query_type": "update",
  "confidence": "high",
  "model_used": "local",
  "requires_confirmation": true,
  "auto_executed": false,
  "warning": "⚠️ This query will UPDATE existing records in 'users'. Ensure conditions are correct to avoid unintended changes.\n⚠️ This will affect 1 row.\nAffected columns: name, email",
  "dry_run": {
    "count_query": "SELECT COUNT(*) FROM users WHERE id = 10",
    "estimated_rows": 1,
    "affected_columns": ["name", "email"]
  }
}
```

---

## Observability

DB Buddy tracks comprehensive metrics to enable debugging, optimization, and trust in AI-generated queries:

- **Query success rate** - Tracks successful vs failed query executions
- **Model usage patterns** - Monitors local vs Nemotron fallback usage
- **Confidence distribution** - Analyzes high/medium/low confidence percentages
- **Average latency** - Tracks query execution performance
- **Query history** - Maintains recent query history with success rates
- **Similar query detection** - Suggests previously used queries for efficiency

This observability enables continuous improvement and provides transparency into AI behavior.

---

## Safety UI Example

When executing destructive queries, DB Buddy provides a clear safety interface:

```
User: Delete user with id 5

⚠️ This query will DELETE rows from 'users'. This action cannot be undone.
⚠️ This will affect 1 row.

[Run Query]   [Cancel]
```

For UPDATE queries, execution diff awareness shows affected columns:

```
User: Update user name and email

⚠️ This query will UPDATE existing records in 'users'. Ensure conditions are correct to avoid unintended changes.
⚠️ This will affect 1 row.
Affected columns: name, email

[Run Query]   [Cancel]
```

---

## Quick start

**Prerequisites:** Python 3.10+, MySQL, [Ollama](https://ollama.com) (optional, for local AI)

### Requirements

- Python 3.10 or higher
- MySQL 5.7+ or MariaDB 10.3+
- Ollama (optional, for local AI - Qwen Coder model)
- Nemotron API key (optional, for cloud fallback)
- pip for package management

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
| `local` | Qwen Coder via Ollama | Default. Runs fully offline, no API key needed |
| `nemotron` | NVIDIA Nemotron | Cloud. Requires API key for complex queries |
| `hybrid` | local → Nemotron | Local first; falls back to Nemotron only if local is unreachable or returns invalid SQL |

**Hybrid priority rule:** DB Buddy always tries the local Qwen Coder model first in hybrid mode. It only falls back to Nemotron if the local model is unreachable, times out, or returns an unusable result.

### Local AI setup (Ollama)

```bash
ollama pull qwen-coder
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

237+ tests pass, including property-based tests via [Hypothesis](https://hypothesis.works), adversarial security tests, fuzz testing, and chaos testing:

- DB connection success/failure/exception paths
- Schema fetching (DESCRIBE called exactly once per table, completeness, exception handling)
- Semantic mapping (exact match, substring match, case-insensitive, longest keyword wins)
- Output writing (atomic temp-file pattern, indentation, overwrite, error propagation)
- CLI behavior (re-prompts, defaults, exit codes)
- AI integration (success, timeout, caching, batch processing, provider routing)
- Plugin loader (valid plugin, invalid fallback, classify interface)
- Query safety (READ auto-execution, WRITE confirmation, dry-run previews)
- Pipeline resilience (chaos testing, fuzz testing, adversarial inputs)

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
