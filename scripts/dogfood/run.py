"""Dogfood loop: ask the engine real questions against a populated database.

    python scripts/dogfood/run.py                # build the DB if needed, run all suites
    python scripts/dogfood/run.py --suite fanout # one suite
    python scripts/dogfood/run.py --rebuild -v   # fresh DB, show every probe's SQL

Exit code is non-zero when there are findings, so this can gate a change once the
loop is quiet.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from dbbuddy_core.models import DBConfig  # noqa: E402
from scripts.dogfood import (  # noqa: E402
    dataset, dataset_hospital, dataset_legacy, dataset_employees, dataset_adventureworks,
    dataset_tpch, dataset_tpcds, dataset_airportdb,
    suites, suites_hospital, suites_legacy, suites_employees, suites_adventureworks,
    suites_tpch, suites_tpcds, suites_airportdb,
)
from scripts.dogfood.invariants import Probe, Report  # noqa: E402
from scripts.dogfood.sqlite_target import engine_pointed_at, query  # noqa: E402

# Each entry pairs a schema generator with the suites written for it. Suites are
# per-dataset because the golden-value tier needs real SQL against real tables;
# the invariant tier would port unchanged.
DATASETS = {
    "erp": (dataset, suites.SUITES, "dbbuddy_dogfood.db"),
    "hospital": (dataset_hospital, suites_hospital.SUITES, "dbbuddy_dogfood_hospital.db"),
    "legacy": (dataset_legacy, suites_legacy.SUITES, "dbbuddy_dogfood_legacy.db"),
    "employees": (dataset_employees, suites_employees.SUITES, "dbbuddy_dogfood_employees.db"),
    "adventureworks": (dataset_adventureworks, suites_adventureworks.SUITES, "dbbuddy_dogfood_adventureworks.db"),
    "tpch": (dataset_tpch, suites_tpch.SUITES, "dbbuddy_dogfood_tpch.db"),
    "tpcds": (dataset_tpcds, suites_tpcds.SUITES, "dbbuddy_dogfood_tpcds.db"),
    # Tier via $AIRPORTDB_TIER (s|m|l) — see dataset_airportdb.
    "airportdb": (dataset_airportdb, suites_airportdb.SUITES, "dbbuddy_dogfood_airportdb.db"),
}

DEFAULT_DB = os.path.join(tempfile.gettempdir(), "dbbuddy_dogfood.db")

# Redis namespaces the engine reads on the query path. Cleared before a run so a
# finding reflects the code, not a previously cached answer.
CACHE_PREFIXES = ("plan", "query", "schema", "chartrun", "response")


def clear_engine_caches() -> list[str]:
    try:
        from dbbuddy_core.context_store import _get_cache

        cache = _get_cache()
        if cache is None or not getattr(cache, "connected", False):
            return []
        cleared = []
        for prefix in CACHE_PREFIXES:
            try:
                cache.clear_prefix(prefix)
                cleared.append(prefix)
            except Exception:  # noqa: BLE001
                pass
        return cleared
    except Exception:  # noqa: BLE001 — no Redis is a fine state
        return []


def make_config(db_path: str, target: str = "sqlite") -> DBConfig:
    # ai=False: the rule-based engine is what is under test. An LLM in the loop
    # would make failures non-reproducible and hide which layer is wrong.
    #
    # engine="mysql" keeps the dialect under test stable across datasets; the
    # sqlite shim translates MySQL's backtick quoting to the double quotes SQLite
    # accepts (see sqlite_target._Cursor.execute). Switching the declared engine
    # instead would change operator and function rendering too, which is a
    # different experiment.
    #
    # ``--target postgres`` is the other half of that trade. It declares the
    # engine as PostgreSQL and rewrites nothing, so the statement the compiler
    # emits is the statement the database sees. That is the only way to grade the
    # SQL itself: the SQLite shim's backtick-to-double-quote rewrite turned an
    # aggregate quoted as an identifier into a string constant, which executes,
    # so eight datasets stayed green while "top N X by Y" was broken on
    # PostgreSQL. See scripts/dogfood/postgres_target.py.
    if target == "postgres":
        from scripts.dogfood.postgres_target import pg_params

        params = pg_params()
        return DBConfig(host=params["host"], port=params["port"], user=params["user"],
                        password=params["password"], database=params["database"],
                        engine="postgresql", ai=False)

    return DBConfig(host="localhost", user="dogfood", password="",
                    database=db_path, engine="mysql", ai=False)


def ask(config, question: str) -> Probe:
    """Run one natural-language question through the full pipeline."""
    from dbbuddy_core.pipeline import process_query

    try:
        result = process_query(config, question, auto_execute_reads=True)
    except Exception as exc:  # noqa: BLE001 — a crash is a finding, not a stop
        return Probe(question=question, error=f"{type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return Probe(question=question, error=f"non-dict result: {type(result)}")

    sql = result.get("sql")
    if result.get("error"):
        return Probe(question=question, sql=sql, error=str(result["error"]),
                     meta={"confidence": result.get("confidence")})
    if not result.get("auto_executed"):
        return Probe(question=question, sql=sql,
                     error="not auto-executed (held or classified as a write)")
    return Probe(question=question, sql=sql, rows=result.get("results") or [],
                 meta={"confidence": result.get("confidence"),
                       "ambiguities": result.get("ambiguities") or [],
                       "model": result.get("model_used")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="erp", choices=sorted(DATASETS),
                    help="which schema to exercise")
    ap.add_argument("--db", default=None)
    ap.add_argument("--target", default="sqlite", choices=("sqlite", "postgres"),
                    help="where the dataset runs. sqlite (default) is fast and needs no "
                         "server but cannot grade the SQL itself; postgres copies the "
                         "dataset into a real server and rewrites nothing. Connection "
                         "from DOGFOOD_PG_* env vars. Use it for shape, not scale — the "
                         "copy is row-by-row.")
    ap.add_argument("--rebuild", action="store_true", help="regenerate the dataset")
    ap.add_argument("--suite", nargs="+", help="run only these suites")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every probe")
    ap.add_argument("--keep-cache", action="store_true",
                    help="do NOT clear cached plans/responses first (debugging only)")
    ap.add_argument("--shuffle", nargs="?", const=-1, type=int, metavar="SEED",
                    help="run suites in a randomized order to surface order/shared-state "
                         "dependencies. Optional integer SEED for reproducibility; "
                         "omit it for a random seed (printed so a failure can be replayed).")
    args = ap.parse_args()

    # A correctness loop must never be served from cache. Plans and responses are
    # cached in Redis keyed by question + schema hash, and Redis outlives the
    # process — so without this the harness grades whatever the engine answered
    # *before* the fix under test, and a real fix looks like it did nothing. This
    # cost two iterations to notice, because a brand-new question proves the fix
    # works while every previously-asked one keeps failing.
    if not args.keep_cache:
        cleared = clear_engine_caches()
        if cleared:
            print(f"cleared cached plans/responses: {', '.join(cleared)}")

    dataset_mod, available, default_name = DATASETS[args.dataset]
    db_path = args.db or os.path.join(tempfile.gettempdir(), default_name)
    args.db = db_path

    if args.rebuild or not os.path.exists(db_path):
        print(f"building '{args.dataset}' dataset at {db_path} …")
        try:
            dataset_mod.build(db_path)
        except Exception:
            # A failed build leaves a partial database behind, and the next run
            # sees the file, skips rebuilding, and grades the engine against a
            # schema missing most of its tables. That reads as the *engine*
            # failing to detect tables — it cost an iteration. Remove the
            # half-built file so the failure cannot be inherited.
            if os.path.exists(db_path):
                os.remove(db_path)
            raise
    counts = dataset_mod.stats(db_path)
    print(f"dataset '{args.dataset}': {sum(counts.values()):,} rows "
          f"across {len(counts)} tables")
    chosen = {k: v for k, v in available.items()
              if not args.suite or k in args.suite}
    if not chosen:
        print(f"no such suite; available: {', '.join(available)}", file=sys.stderr)
        return 2

    # Suite order is a shared-state probe: the context, caches and semantic memory
    # persist across suites in one process, so a bug where one query's state leaks
    # into the next only shows when the order changes. A green run here means the
    # result does not depend on which question was asked first.
    if args.shuffle is not None:
        import random
        seed = args.shuffle if args.shuffle != -1 else random.randrange(1 << 30)
        items = list(chosen.items())
        random.Random(seed).shuffle(items)
        chosen = dict(items)
        print(f"shuffled suite order (seed {seed}): {', '.join(chosen)}")

    config = make_config(args.db, args.target)
    report = Report()
    started = time.time()

    if args.target == "postgres":
        from scripts.dogfood.postgres_target import (
            engine_pointed_at_postgres, load_sqlite_into_postgres,
        )
        from scripts.dogfood.postgres_target import query as pg_query

        print(f"copying {args.dataset} into PostgreSQL "
              f"({config.host}:{config.port}/{config.database}) …")
        copied = load_sqlite_into_postgres(args.db)
        print(f"  {copied} rows")
        target_ctx = engine_pointed_at_postgres()
        run_sql = pg_query

        def list_columns(table):
            rows = pg_query(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = 'public' AND table_name = '{table}' "
                "ORDER BY ordinal_position"
            )
            return [r["column_name"] for r in rows]
    else:
        target_ctx = engine_pointed_at(args.db)
        run_sql = lambda s: query(args.db, s)   # noqa: E731

        def list_columns(table):
            return [r["name"] for r in query(args.db, f'PRAGMA table_info("{table}")')]

    with target_ctx:
        # Analyze first, like a real user does. The dimension value index — which
        # is what lets a bare literal ("shipped") bind to the column that holds it
        # — is only built on an explicit analyze/rebuild, so querying a
        # never-analyzed database exercises a state the product does not ship in
        # and blames the planner for a missing index.
        from dbbuddy_core.pipeline import process_schema

        try:
            process_schema(config)
        except Exception as exc:  # noqa: BLE001
            print(f"  analyze failed: {type(exc).__name__}: {exc}")

        for name, suite in chosen.items():
            print(f"\n── {name} " + "─" * max(0, 60 - len(name)))
            ctx = suites.SuiteContext(
                ask=lambda q: ask(config, q),
                sql=run_sql,
                columns=list_columns,
                report=report,
                verbose=args.verbose,
            )
            try:
                suite(ctx)
            except Exception as exc:  # noqa: BLE001
                print(f"  suite crashed: {type(exc).__name__}: {exc}")
                report.findings.append(
                    suites.crash_finding(name, exc))

    elapsed = time.time() - started
    print("\n" + "=" * 70)
    for finding in report.findings:
        print(finding.describe())
        print()
    print(f"{report.summary()}  ({elapsed:.1f}s)")
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
