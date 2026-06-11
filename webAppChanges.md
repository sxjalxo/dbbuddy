# webAppChanges.md

## Phase 1 — Understanding the toolkit
- Reviewed the Python CLI, core engine, FastAPI backend, and frontend structure.
- Confirmed the project should stay thin at the CLI/API boundary and reusable in the core layer.

## Phase 2 — Core cleanup
- Centralized semantic mapping and schema analysis under `dbbuddy_core/`.
- Kept `dbbuddy/main.py` thin and aligned the core path with typed `DBConfig` usage.

## Phase 3 — Backend API
- Added `/analyze` for schema analysis.
- Added CORS so the frontend can talk to the backend directly.

## Phase 4 — Frontend integration
- Connected the UI to backend analysis.
- Improved the semantic output panel with grouped intelligence, AI-source cues, and export/search polish.

## Phase 5 — Query path
- Added SQL generation and safe execution support through `dbbuddy_core/query.py`.
- Added `/query` and `/execute` endpoints for AI-grounded query generation and manual execution.

## Phase 6 — Reliability layer
- Added SQL validation, safe execution, AI-driven SQL repair, and confidence scoring.
- SELECT queries can now auto-run; failed SELECT attempts trigger an automatic fix-and-retry loop.
- Non-SELECT queries stay in a review/approval path for safety.

## Phase 8 — Ollama and provider routing
- Added local Ollama SQL generation and SQL repair helpers.
- Added provider-aware routing for `local`, `openai`, and `hybrid` models.
- Added Ollama availability detection and fallback warnings in the pipeline.

## Outcome
- The system now combines semantic grounding, safe SQL generation, automatic retry on failures, and a clear safety boundary for destructive actions.

## Phase 9 — Productization

- Rewrote README to reflect the full stack: semantic layer, AI providers, hybrid routing, safety model, plugin system, full architecture.
- Fixed remaining `dbbuddy.plugins.*` stale imports in `dbbuddy_core/mapping.py`, `dbbuddy_core/plugins/loader.py`, `dbbuddy_core/plugins/default_mapping.py`, and `dbbuddy_core/pipeline.py` — all plugin paths now correctly go through `dbbuddy_core.plugins`.
- Removed dead `write_output` call from CLI `main()` and stripped the unused `os` import; `write_output` function retained as a tested utility.
- Removed Lovable scaffolding from the frontend: deleted `.lovable/`, removed `@lovable.dev/vite-tanstack-config` devDependency, replaced with standard `vite` + `@vitejs/plugin-react` + `@tailwindcss/vite` + `@tanstack/react-start/plugin/vite` + `vite-tsconfig-paths`. Deleted `src/lib/lovable-error-reporting.ts` and all references.
- Added favicon support: created `frontend/public/` directory and added `<link rel="icon">` in `__root.tsx`.
- Confirmed hybrid AI priority: local Ollama is always tried first across `generate_sql`, `fix_sql`, `classify_column`, `batch_classify_columns`, and `ai_refine`. OpenAI is only reached when Ollama is unreachable, times out, or returns `unknown`/empty.
- All 96 tests pass, 0 regressions.

## Phase 10 — Structure cleanup

- Deleted duplicate `dbbuddy/plugins/` folder — plugins now live exclusively in `dbbuddy_core/plugins/`.
- Updated all test imports from `dbbuddy.plugins.*` → `dbbuddy_core.plugins.*`.
- Renamed `Frontend/` → `frontend/` to match `backend/` casing convention.
- Deleted stale root-level `dbbuddy.log` — all logs now go to `logs/dbbuddy.log`.
- Updated `dbbuddy/main.py` logging to use explicit `FileHandler("logs/dbbuddy.log")` + `StreamHandler()` so logs go to both file and terminal.
- Added `[tool.black]` and `[tool.isort]` sections to `pyproject.toml`.
- Added `[project.optional-dependencies] dev` group to `pyproject.toml` — source of truth for all deps.
- Created root `.gitignore` covering Python, Node, logs, secrets, editor artefacts.
- Added Key Highlights section, ASCII data-flow diagram, and Design Principles to README.
- Added demo GIF placeholder to README (`docs/demo.gif`).
