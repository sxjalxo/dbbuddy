# dbbuddy

A Python CLI tool that connects to a MySQL database, reads its schema metadata, maps column names to generalized semantic terms using a plugin system, and outputs the result to `output.json`.

## Overview

dbbuddy analyzes database schemas by classifying column names into semantic categories like "value", "quantity", "name", "date", "identifier", "status", and "description". This helps understand the purpose of database columns without manual inspection.

## Features

- **Interactive CLI**: Prompts for database connection details (host, username, password, database name)
- **Schema Extraction**: Automatically fetches all tables and columns using `SHOW TABLES` and `DESCRIBE`
- **Semantic Mapping**: Classifies column names using a pluggable mapping system with default rules
- **Case-Insensitive Matching**: Works with any casing (e.g., "AMT", "Amount", "amt" all map to "value")
- **Smart Substring Matching**: Longest keyword wins to prevent shadowing (e.g., "uuid" before "id")
- **Atomic Output**: Writes to `output.json` using a temp-file pattern for safety
- **Comprehensive Testing**: 95 tests including property-based tests with Hypothesis

## Installation

**Prerequisites:**
- Python 3.8+
- MySQL database installed and running
- A MySQL database with tables to analyze

1. Clone the repository
2. Create a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # Linux/Mac
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Install the tool:
   ```bash
   pip install -e .
   ```

## Usage

Run the tool:

**Windows (if Scripts directory not in PATH):**
```bash
.venv\Scripts\dbbuddy.exe
```

**Or add Scripts directory to PATH:**
```bash
set PATH=%PATH%;%CD%\.venv\Scripts
dbbuddy
```

You will be prompted for MySQL database credentials:
- **Host** (default: `localhost`)
- **Username** (required) - MySQL username
- **Password** (masked input) - MySQL password
- **Database name** (required) - MySQL database name

**Note:** dbbuddy connects to an existing MySQL database. You must have MySQL installed and configured with a database before running the tool. The username and password are your MySQL database credentials, not dbbuddy tool credentials.

Example session:
```
[*] Using mapping plugin: default_mapping
Host [localhost]: 
Username: root
Password: 
Database: mydb
Connected to MySQL database successfully
Schema fetched successfully
Output written to: C:\Users\Sujal\Projects\dbbuddy\output.json
```

## Output Format

The tool generates `output.json` with the following structure:

```json
{
  "users": {
    "id": {
      "term": "identifier",
      "source": "rule",
      "plugin": "Plugin"
    },
    "name": {
      "term": "name",
      "source": "rule",
      "plugin": "Plugin"
    },
    "email": {
      "term": "unknown",
      "source": "rule",
      "plugin": "Plugin"
    },
    "created_at": {
      "term": "date",
      "source": "rule",
      "plugin": "Plugin"
    }
  },
  "orders": {
    "order_id": {
      "term": "identifier",
      "source": "rule",
      "plugin": "Plugin"
    },
    "amount": {
      "term": "value",
      "source": "rule",
      "plugin": "Plugin"
    },
    "status": {
      "term": "status",
      "source": "rule",
      "plugin": "Plugin"
    },
    "updated_at": {
      "term": "date",
      "source": "rule",
      "plugin": "Plugin"
    }
  }
}
```

**Explainability:** Each column mapping includes:
- `term`: The semantic classification
- `source`: Either `"rule"` (plugin mapping) or `"ai"` (AI classification)
- `plugin`: The name of the plugin that performed the classification

## Semantic Mapping Rules

The default plugin maps 27 keywords to 7 semantic terms:

| Keywords | Semantic Term |
|----------|---------------|
| amt, amount, price, cost, total, revenue | value |
| qty, quantity, count, num, number | quantity |
| name, title, label | name |
| date, time, created_at, updated_at, timestamp | date |
| id, uuid, key | identifier |
| status, state, flag | status |
| desc, description, note, comment | description |

**Matching priority:**
1. Exact match (case-insensitive) takes priority
2. Substring match with longest keyword first
3. No match → "unknown"

## Architecture

```
User Input (CLI)
      ↓
  Config Loader
      ↓
  Plugin Loader
      ↓
  DB_Connector (db.py)
      ↓
  Schema_Fetcher (main.py)
      ↓
  Semantic_Mapper (plugin)
      ↓
  AI_Mapper (optional)
      ↓
  Output_Writer (main.py)
      ↓
  output.json
```

## File Structure

```
dbbuddy/
├── __init__.py
├── __main__.py
├── main.py
├── db.py
└── plugins/
    ├── __init__.py
    ├── base.py
    ├── default_mapping.py
    └── loader.py
tests/
├── test_main.py
├── test_db.py
└── __init__.py
pyproject.toml
requirements.txt
```

## Testing

Run all tests:
```bash
pytest tests/ -v
```

The test suite includes:
- **Unit tests**: Specific scenarios and edge cases using `unittest.mock`
- **Property-based tests**: Formal correctness properties validated with Hypothesis (100 iterations each)

Test coverage includes:
- Connection success/failure paths
- Schema fetching with empty/normal/exception cases
- Semantic mapping (exact match, substring match, unknown)
- Output writing (indentation, overwrite, error handling)
- CLI behavior (re-prompts, defaults, exit codes)
- AI integration (success, timeout, validation, caching)
- Logging events (AI classification, failures)

## Logging

DB Buddy writes structured logs to `logs/dbbuddy.log` for operational visibility:

**Log format:**
```
YYYY-MM-DD HH:MM:SS | LEVEL | Message
```

**Log levels:**
- `INFO`: Normal flow (connection, schema fetch, output write, AI classification)
- `WARNING`: Recoverable issues (AI classification failure)
- `ERROR`: Critical failures (connection, schema fetch, output write)

**Example log output:**
```
2026-06-06 18:10:12 | INFO | Database connection established
2026-06-06 18:10:12 | INFO | Schema fetched: 3 tables
2026-06-06 18:10:13 | INFO | Generating semantic layer output
2026-06-06 18:10:13 | INFO | AI refining column: email
2026-06-06 18:10:14 | INFO | AI classified email -> contact
2026-06-06 18:10:15 | INFO | Output written to C:\...\output.json
```

**Logging principles:**
- Logs at pipeline boundaries only (minimal but powerful)
- No sensitive data (passwords never logged)
- Structured format suitable for SIEM integration
- Zero impact on existing business logic

## Configuration

dbbuddy supports configuration via JSON file for scriptable, repeatable deployments:

**Usage modes:**
1. Interactive (default): `.venv\Scripts\dbbuddy.exe` or `dbbuddy` (if Scripts in PATH)
2. Config-driven: `.venv\Scripts\dbbuddy.exe --config config.json`
3. Hybrid override: `.venv\Scripts\dbbuddy.exe --config config.json --ai-provider openai`

**Configuration priority:** CLI args > Config file > Defaults

**Example config.json:**
```json
{
  "host": "localhost",
  "user": "root",
  "database": "testdb",
  "ai": true,
  "ai_provider": "hybrid"
}
```

**Configuration fields:**
- `host`: Database host (default: prompt or "localhost")
- `user`: Database username (default: prompt)
- `database`: Database name (default: prompt)
- `output`: Output file path (default: "output.json")
- `ai`: Enable AI classification (default: false)
- `ai_provider`: AI provider - "local", "openai", or "hybrid" (default: "local")
- `mapping_plugin`: Mapping plugin to use (default: "default_mapping")

**Security note:** Password is never stored in config file - always prompted securely via getpass.

**Error handling:**
- Missing config file → exits with error
- Invalid JSON → exits with error
- Invalid provider → exits with error
- Invalid plugin → falls back to default_mapping

## Plugin System

dbbuddy supports a plugin system for custom mapping logic. Plugins allow you to extend or replace the default column classification rules without modifying the core code.

**Plugin structure:**
```
dbbuddy/plugins/
├── __init__.py
├── base.py              # Base MappingPlugin interface
├── default_mapping.py   # Default implementation
└── loader.py            # Dynamic plugin loader
```

**Creating a custom plugin:**
```python
# dbbuddy/plugins/custom_mapping.py
from dbbuddy.plugins.base import MappingPlugin

class Plugin(MappingPlugin):
    MAP = {
        "amount": "value",
        # Your custom rules here
    }
    
    def classify(self, column_name: str) -> str:
        col = column_name.lower()
        if col in self.MAP:
            return self.MAP[col]
        return "unknown"
```

**Using a custom plugin:**
```json
{
  "mapping_plugin": "custom_mapping"
}
```

**Plugin interface:**
All plugins must inherit from `MappingPlugin` and implement the `classify(column_name: str) -> str` method.

## Local AI Setup (Ollama)

dbbuddy uses Ollama for local AI classification by default.

**Prerequisites:**
- Install Ollama: https://ollama.com

**Setup:**
```bash
# Pull a model
ollama pull llama3

# Start Ollama server
ollama serve

# Or simply run (starts server automatically)
ollama run llama3
```

**Usage:**
```bash
.venv\Scripts\dbbuddy.exe --ai
```

**Note:** Ollama runs locally at http://localhost:11434 and does not require an API key. If Ollama is not running, dbbuddy will log a warning and fall back to "unknown" for AI classifications.

## AI Integration

The `--ai` flag enables AI integration to classify "unknown" columns with support for multiple providers:

**Usage:**
```bash
# Local Ollama (default, no API key needed)
.venv\Scripts\dbbuddy.exe --ai

# OpenAI API (requires OPENAI_API_KEY)
set OPENAI_API_KEY=your_api_key  # Windows
export OPENAI_API_KEY=your_api_key  # Linux/Mac
.venv\Scripts\dbbuddy.exe --ai --ai-provider openai

# Hybrid (local first, fallback to OpenAI)
.venv\Scripts\dbbuddy.exe --ai --ai-provider hybrid
```

**Providers:**
- `local` (default): Uses local Ollama LLM (llama3) at http://localhost:11434
- `openai`: Uses OpenAI API (gpt-4o-mini) - requires `OPENAI_API_KEY`
- `hybrid`: Tries local first, falls back to OpenAI if local returns "unknown" - requires `OPENAI_API_KEY`

**Behavior:**
- Sends all "unknown" columns to selected AI provider in a single batch for semantic classification
- Uses 5-second timeout for local, 10-second timeout for OpenAI
- Validates response: must be non-empty, single-word, alphanumeric only
- Falls back to "unknown" on failure, timeout, or invalid response
- Prints batch progress (number of columns being classified)
- Caches results to avoid repeated API calls for same column name
- Marks AI-classified columns with the actual provider name (e.g., "openai", "local", "hybrid") in output
- Logs provider selection and batch classification results with provider prefix
- **Performance gain:** 100 columns → 1-3 API calls instead of 100 separate calls

**Example output with AI:**
```json
{
  "users": {
    "id": {
      "term": "identifier",
      "source": "rule",
      "plugin": "Plugin"
    },
    "email": {
      "term": "contact",
      "source": "openai",
      "plugin": "Plugin"
    }
  }
}
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Repository

[GitHub Repository](https://github.com/sxjalxo/dbbuddy.git)