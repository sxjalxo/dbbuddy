# Contributing to DB Buddy

Thanks for considering a contribution. DB Buddy is a deterministic NL→SQL engine, and
"deterministic" is a promise the codebase has to keep — most of the conventions below
exist to protect it. Read the [Non-negotiables](#non-negotiables) before writing code.

- **Deeper orientation:** [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) — a codebase
  map answering "where is X implemented?" for most of the system.
- **Architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Security model:** [docs/SECURITY.md](docs/SECURITY.md)

---

## Quick start

**Prerequisites:** Python 3.10+, Node 20+, and (optionally) Docker for live-database tests.

```bash
git clone https://github.com/sxjalxo/dbbuddy.git
cd dbbuddy

python -m venv .venv
source .venv/bin/activate     # Linux/macOS
.venv\Scripts\activate        # Windows

pip install -r requirements.txt
pip install -e ".[dev]"
```

Validate the install with no database required:

```bash
python scripts/run_validation.py
```

Run the test suite:

```bash
pytest
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Backend and demo-account seeding are covered in
[docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md#setup).

> Use the project `.venv` interpreter. Runtime dependencies (chromadb, DB drivers)
> live there; the system Python will not have them.

---

## Non-negotiables

These are not style preferences. A PR that violates one will be asked to change.

### 1. No hardcoded schema knowledge

The engine must never contain a table name, column name, or domain value from any
specific database. Everything is derived at runtime from the schema — column roles,
primary keys, foreign keys, the value index.

If a fix needs to know something about the data, derive it from schema metadata or add
it to the type/role framework. A hardcoded `"revenue"` or `"customers"` looks like a fix
and is a bug that ships to every other user.

See [`dbbuddy_core/type_handlers.py`](dbbuddy_core/type_handlers.py) for the
extension point for value comparison, and
[`dbbuddy_core/semantic_roles.py`](dbbuddy_core/semantic_roles.py) for role inference.

### 2. Planner changes bump `PLAN_VERSION`

Any change to planning output invalidates cached plans and snapshot expectations. Bump
`PLAN_VERSION` in [`dbbuddy_core/query_planner.py`](dbbuddy_core/query_planner.py) in the
same commit as the behavior change, never after, and note what changed in the trailing
comment the way the existing versions do.

### 3. Prompts are guidance; validators are enforcement

Model output that a user acts on must be validated **after** generation, not merely
requested politely in a prompt. See
[docs/SECURITY.md](docs/SECURITY.md#ai-output-validation-prompt-injection) and
[`dbbuddy_core/insights/validators.py`](dbbuddy_core/insights/validators.py) for the
worked example.

### 4. Every path that queries a target database holds a `query_slot()`

[`dbbuddy_core/erp_concurrency.py`](dbbuddy_core/erp_concurrency.py) is the only real
ceiling on concurrent sessions against a connected database — the connection pool bounds
*idle* connections only. A new code path that skips the semaphore makes the ceiling
decorative for everyone.

### 5. Taper, don't cut off

When a heuristic produces a weak result, down-weight it — do not silently drop it. A hard
threshold that deletes borderline results reads to the user as the product being broken.

### 6. Missing capability ≠ defect

If the engine cannot yet express a query shape, that is a feature request, not a bug to
paper over with a special case. Say so in the issue and design the general mechanism.

---

## Testing

| Command | What it covers |
|---|---|
| `pytest` | Full unit suite (integration tests excluded by default) |
| `DBBUDDY_STRICT=1 pytest` | Same, but contract violations raise instead of being silently recovered — **use this before opening a PR** |
| `pytest -m integration` | Live MySQL / PostgreSQL / SQL Server — opt in, needs Docker |
| `python scripts/dogfood/run.py --dataset erp` | Row-level correctness against a populated database |
| `python scripts/benchmarks/run_all.py --check` | Performance gate against the committed baseline |
| `ruff check .` | Lint |

Live databases for the integration suite:

```bash
docker compose -f docker-compose.test.yml up -d
pytest -m integration
docker compose -f docker-compose.test.yml down -v
```

### Dogfood suites — run them one at a time

Each dogfood process loads the embedding stack, and the large datasets scan millions of
rows. **Run one dataset per process, serially.** Concurrent runs cost more memory than a
normal development machine has.

`run.py --shuffle` randomizes suite order to catch order and shared-state dependence.

### What a good test looks like

- Correctness fixes ship with a dogfood case or a
  [`tests/schema_portability/`](tests/schema_portability/) case, not only a unit test —
  the point is that the fix holds on schemas nobody anticipated.
- New dialects implement the shared contract in
  [`tests/test_dialect_contract.py`](tests/test_dialect_contract.py). See
  [docs/ADDING_DATABASE_DIALECT.md](docs/ADDING_DATABASE_DIALECT.md).
- Never add a test to a skip list to make a suite green. A skip list here has already
  hidden three live SQL join defects; that is why there isn't one any more.

---

## Pull requests

1. Branch from `main`. One logical change per PR.
2. `DBBUDDY_STRICT=1 pytest` and `ruff check .` pass locally.
3. Fill in the PR template — especially *how you verified it*. "Tests pass" is not
   verification of a correctness fix; naming the dogfood case that now passes is.
4. Update docs in the same PR. If you changed where something lives, update the codebase
   map in [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md).
5. Add a `CHANGELOG.md` entry under `[Unreleased]` for anything user-visible.

### Commit messages

Conventional Commits — `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `perf:`, `chore:`.
Explain *why* in the body; the diff already shows *what*.

### Dependencies

`requirements.txt` holds floors; `requirements.lock` is the compiled, pinned
resolution of them. Change the first, then regenerate the second:

```bash
uv pip compile --universal --python-version 3.12 requirements.txt -o requirements.lock
```

`--universal` matters: without it the lock carries whichever platform you compiled
on, and CI cannot install it.

### Code style

- **Python:** ruff, 100-column lines. Type hints on public functions.
- **TypeScript:** Prettier + ESLint (`npm run format`, `npm run lint`).
- **Comments** explain reasoning and constraints, not syntax. The existing security and
  concurrency comments are the house standard — a comment that records *why a naive
  version is wrong* is worth more than three that restate the code.

---

## Reporting issues

- **Wrong SQL / wrong answer** — use the *Wrong answer* issue template. A reproduction as
  a dogfood case is the fastest route to a fix, and becomes a permanent regression test.
  Without a reproducible schema, a wrong-answer report is unfalsifiable and will likely
  stall.
- **Security vulnerability** — do **not** open a public issue. See
  [SECURITY.md](SECURITY.md).
- **Feature request** — describe the query shape or workflow you need, not the
  implementation you imagine.

---

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you
agree to uphold it.

## License

Contributions are licensed under [Apache License 2.0](LICENSE), the project's license.
