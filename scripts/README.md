# Developer scripts

Ad-hoc developer/ops utilities. Not part of the app runtime. Run from the repo
root with the project interpreter (the package is installed editable, so
`dbbuddy_core` imports resolve):

| Script | What it does |
| ------ | ------------ |
| `debug_memory.py` | Prints the current semantic-learning memory state (mappings, column/table usage) — useful when debugging the learning subsystem. |
| `run_validation.py` | Runs the `SystemValidator` suite (determinism, cache consistency, adversarial queries, latency). Works out of the box against a built-in demo schema; pass `--schema <file.json>` or live-DB flags to validate a real schema. Exit 0 if the core checks pass. |
| `benchmarks/` | Performance suite — one file per subsystem, plus an end-to-end pipeline run. See below. |
| `dogfood/` | Correctness suite — runs the engine against populated databases and scores answers with **invariants** rather than expected values. `run.py --dataset erp\|hospital\|legacy\|employees\|adventureworks\|tpch\|tpcds\|airportdb` — run one at a time. See [DEVELOPER_GUIDE.md](../docs/DEVELOPER_GUIDE.md#correctness-loop-dogfooding). |
| `exercise_schemas.py` | Exploratory harness: runs the schema-adaptive layers (type classification, FK/relationship inference, filter/name-literal extraction) against realistic ERP/domain schemas (SAP, Odoo, ERPNext, healthcare, …) and prints a capability matrix. Not a pass/fail test — used to find where the heuristics break. |

```bash
.venv/Scripts/python.exe scripts/run_validation.py      # Windows (built-in demo schema)
.venv/Scripts/python.exe scripts/exercise_schemas.py    # capability matrix across schemas
# .venv/bin/python  scripts/debug_memory.py             # POSIX
```

For the automated test suite, use `pytest` (see [../docs/DEVELOPER_GUIDE.md](../docs/DEVELOPER_GUIDE.md)).
## Correctness dogfood (`dogfood/`)

The dogfood runner drives the full NL→SQL pipeline against populated SQLite
targets and clears cached plans/responses before each run unless `--keep-cache`
is passed. The first three datasets are generated from Python. The `employees`
dataset imports the canonical MySQL employees sample dumps from
`data/employees/dumps` (or `$EMPLOYEES_DUMP_DIR`), which are deliberately
gitignored because they are large.

```bash
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset erp
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset employees --rebuild
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset employees --suite fanout having
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset erp --shuffle
```

The suite exits non-zero when it finds a crash, a failed invariant, a golden-value
miss, or a confidence problem.
## Benchmarks (`benchmarks/`)

One file per subsystem so a regression names its own cause, rather than a single
number that says "something got slower". All run on a synthetic SQLite schema —
no live DB, no Redis, no network.

| Benchmark | Isolates | Metric that bites |
| --------- | -------- | ----------------- |
| `bench_embeddings.py` | Vector-store schema search | `uncached_search_p50` — jumps ~8x if the collection stops using the shared embedding function and Chroma rebuilds an ONNX session per call |
| `bench_schema_resolution.py` | Per-query schema read + hash | `fetches_per_resolve` — should be ≈1/workers; goes to 1.0 if single-flight breaks |
| `bench_cache.py` | Redis client, healthy and dead | `dead_redis_call_mean` — near zero while the availability gate works, near the socket timeout when it doesn't |
| `bench_pipeline.py` | Everything end to end | `warm_p50` + per-stage p50 breakdown |

```bash
.venv/Scripts/python.exe scripts/benchmarks/run_all.py            # run + compare to baseline
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --check    # exit 1 on a gated regression
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --update   # re-record baseline.json
.venv/Scripts/python.exe scripts/benchmarks/bench_cache.py        # one subsystem, standalone
```

### Metric kinds

Every metric declares what it is for, so nothing depends on reading a name or a
comment to know whether it blocks a build:

| Kind | In baseline | Compared | Fails `--check` | Use for |
| ---- | ----------- | -------- | --------------- | ------- |
| `gate` | yes | yes | **yes** | numbers whose movement is structural, not ambient |
| `trend` | yes | yes, drift reported | no | worth watching across releases, too environment-dependent to gate (`cold_query`, `index_schema`) |
| `diagnostic` | yes | no | no | context that makes other numbers readable — inputs, counts, avoided costs |
| `experimental` | **no** | no | no | a new metric still earning its place (`response_bytes_p50`) |

`experimental` is a staging area, not a permanent role like `diagnostic`. The
metric is measured and printed below a divider, but never written to
`baseline.json` — nothing can compare against a number that was never recorded,
so it cannot start gating anything before someone decides it should. Every run
names the pending ones, which is the pressure that stops the suite accumulating
numbers nobody trusts but nobody dares delete. Promote to `gate`/`trend` once
stable, or drop it.

The kind is stored in `baseline.json` next to each value, so tooling can answer
"which metrics fail CI?" from the file alone (experimental ones are absent by
design — read them from `--json`, which records the full run):

```bash
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --list-metrics
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --list-metrics --kind gate
```

Trend drift is always printed, just never fatal — a trend metric sliding 3x is
worth seeing even though it must not break the build.

`baseline.json` is committed; a gated metric that drifts past `--tolerance`
(default 1.6x) fails. That threshold is deliberately loose — these run against a
wall clock on a developer machine, and a tight one would flag scheduler noise
until people stopped trusting it. The regressions worth catching are structural
(the two that prompted this suite were 8x and 10x) and clear any noise floor by an
order of magnitude. Re-record the baseline whenever a change moves a number on
purpose.

### Baseline provenance

Wall-clock numbers are only comparable on comparable hardware, so `--update`
stamps `baseline.json` with an `_recorded_on` block — host, platform, processor,
CPU count, Python version, UTC timestamp. When a run reports regressions,
`run_all.py` names the mismatch:

```
NOTE: baseline was recorded on <host> (…, 16 CPUs, python 3.14.0) at <time>;
this run is on <other-host> (…, 8 CPUs, python 3.13.2). Differing: host, cpu_count.
```

That distinction is the whole point: *"this got slower"* and *"this is a different
machine"* otherwise look identical, and the second one costs an afternoon of
bisecting. A baseline recorded before provenance existed reports that it cannot
be checked, rather than staying silent. `_recorded_on` is not a benchmark name and
is skipped when the file is read as `{benchmark: {metric: …}}`.

**Record the baseline on the reference machine**, not on whatever laptop noticed
the drift — `--update` on slow hardware silently lowers the bar for everyone.
