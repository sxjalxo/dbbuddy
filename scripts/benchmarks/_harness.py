"""Shared plumbing for the benchmark suite.

Every benchmark needs the same three things: a synthetic schema of controllable
width, a SQLite stand-in wired into the engine's connection seam, and a way to
report a number that can be compared against a stored baseline. Keeping that in
one place is what lets each benchmark file be about *one subsystem*.

Nothing here imports engine modules at module scope — the persist-directory
environment override below has to land before ``dbbuddy_core.config`` is read.
"""

import json
import os
import platform
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Optional
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bootstrap() -> None:
    """Put the repo on sys.path and redirect throwaway embeddings to scratch.

    Must run before any ``dbbuddy_core`` import: the vector store binds the
    persist directory at import time, and a benchmark has no business writing
    into the developer's real ``chroma_db/``.
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    # Both streams: the logger writes to stderr, and a cp1252 console dies on the
    # em-dashes and arrows in dbbuddy's own log messages.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):  # pragma: no cover - non-tty
            pass
    os.environ.setdefault(
        "CHROMADB_PERSIST_DIRECTORY",
        os.path.join(os.environ.get("TEMP", "/tmp"), "dbbuddy_bench_chroma"),
    )


# ---------------------------------------------------------------- results ----

# How much worse than baseline a metric may drift before `--check` fails.
#
# Deliberately loose. These run on a developer's laptop against a wall clock, so
# a tight gate would flag scheduler noise as a regression and get ignored within
# a week. The regressions worth catching here are structural — a per-call ONNX
# session was 9x, a redundant schema fetch was 10x — and those clear any
# plausible noise floor by an order of magnitude.
DEFAULT_TOLERANCE = 1.6


class MetricKind(str, Enum):
    """What a metric is *for* — declared, not inferred from its name.

    Every metric answers exactly one question, and the three questions have
    genuinely different consequences:

    * ``GATE`` — fails CI. Compared to the baseline, blocks on regression.
      Reserve for numbers whose movement is structural rather than ambient.
    * ``TREND`` — compared and reported as drift, never blocks. For numbers
      worth watching across releases but too noisy or too environment-dependent
      to gate (cold-start costs, one-time index builds).
    * ``DIAGNOSTIC`` — never compared. Context that makes the other numbers
      readable: inputs, counts, the cost that an optimization avoided.
    * ``EXPERIMENTAL`` — measured and printed, but kept **out of the baseline
      entirely** until it has proven itself. A new metric usually needs a few
      releases before anyone knows whether it is stable enough to gate, or even
      whether it measures what its author hoped. Recording it early is how a
      suite accumulates numbers nobody trusts but nobody dares delete.

    ``DIAGNOSTIC`` and ``EXPERIMENTAL`` both skip comparison, but they are not
    the same state: diagnostic is a permanent role, experimental is a staging
    area with an intended exit — promote it to ``GATE`` or ``TREND`` once it has
    earned it, or delete it.

    A str-Enum so the value survives a JSON round-trip as ``"gate"`` and tooling
    can filter on it without importing this module.
    """

    GATE = "gate"
    TREND = "trend"
    DIAGNOSTIC = "diagnostic"
    EXPERIMENTAL = "experimental"


@dataclass
class Metric:
    """One number, plus how to read it and what it is for."""

    name: str
    value: float
    unit: str
    # True when smaller is better (latency); False for throughput.
    lower_is_better: bool = True
    kind: MetricKind = MetricKind.GATE

    @property
    def comparable(self) -> bool:
        """Whether a baseline comparison means anything for this metric."""
        return self.kind in (MetricKind.GATE, MetricKind.TREND)

    @property
    def recordable(self) -> bool:
        """Whether this belongs in baseline.json at all.

        Experimental metrics are deliberately absent: nothing can compare against
        a number that was never written down, so a metric cannot start silently
        gating anything before someone decides it should.
        """
        return self.kind is not MetricKind.EXPERIMENTAL

    def ratio(self, baseline: float) -> Optional[float]:
        """How much worse than baseline, as a factor. <1.0 is an improvement."""
        if baseline <= 0 or self.value <= 0:
            return None
        return (self.value / baseline) if self.lower_is_better else (baseline / self.value)

    def exceeded(self, baseline: float, tolerance: float) -> bool:
        """Whether this drifted past tolerance. Says nothing about blocking."""
        if not self.comparable:
            return False
        r = self.ratio(baseline)
        return r is not None and r > tolerance


@dataclass
class BenchResult:
    """Everything one benchmark produces."""

    benchmark: str
    metrics: list[Metric] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, value: float, unit: str = "ms", *,
            lower_is_better: bool = True, kind: MetricKind = MetricKind.GATE) -> None:
        self.metrics.append(Metric(name, round(value, 3), unit, lower_is_better, kind))

    def to_dict(self) -> dict:
        return {"benchmark": self.benchmark,
                "metrics": [asdict(m) for m in self.metrics],
                "notes": self.notes}

    @property
    def experimental(self) -> list[Metric]:
        return [m for m in self.metrics if m.kind is MetricKind.EXPERIMENTAL]

    def print(self) -> None:
        print(f"\n[{self.benchmark}]")
        established = [m for m in self.metrics if m.kind is not MetricKind.EXPERIMENTAL]
        width = max((len(m.name) for m in self.metrics), default=0)
        for m in established:
            # Gates are unmarked — they are the default and the bulk of the list;
            # tagging the exceptions is what carries information.
            tag = "" if m.kind is MetricKind.GATE else f"  ({m.kind.value})"
            print(f"  {m.name.ljust(width)}  {m.value:>10.3f} {m.unit}{tag}")
        # Below a divider, not interleaved: these are being evaluated, and
        # reading them as part of the result is the mistake to design against.
        if self.experimental:
            print("  " + "· " * (width // 2) + " experimental (not in baseline)")
            for m in self.experimental:
                print(f"  {m.name.ljust(width)}  {m.value:>10.3f} {m.unit}")
        for n in self.notes:
            print(f"  - {n}")


def percentiles(samples: list[float]) -> dict[str, float]:
    """p50/p95/max over a sample list. Empty input yields zeros."""
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0}
    s = sorted(samples)
    return {
        "p50": statistics.median(s),
        "p95": s[max(0, int(len(s) * 0.95) - 1)],
        "max": s[-1],
    }


def time_ms(fn: Callable[[], object]) -> float:
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def repeat_ms(fn: Callable[[], object], n: int) -> list[float]:
    return [time_ms(fn) for _ in range(n)]


# ------------------------------------------------------------- synthetic ----

CORE_TABLES = 3


def build_schema(conn: sqlite3.Connection, filler_tables: int, *, rows: bool = True) -> None:
    """Create three related core tables plus `filler_tables` wide filler tables.

    The filler is the point: every cost that scales with *schema size* — schema
    fetch, hashing, semantic-layer walks, response payload — grows with it, while
    the queries still touch only the core tables. That separation is what makes a
    size-driven regression visible.
    """
    cur = conn.cursor()
    cur.execute("""CREATE TABLE customers (
        id INTEGER PRIMARY KEY, name TEXT COLLATE NOCASE, email TEXT COLLATE NOCASE,
        country TEXT COLLATE NOCASE, city TEXT COLLATE NOCASE, status TEXT COLLATE NOCASE,
        created_at TEXT)""")
    cur.execute("""CREATE TABLE orders (
        id INTEGER PRIMARY KEY, customer_id INTEGER, total_amount REAL,
        status TEXT COLLATE NOCASE, created_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id))""")
    cur.execute("""CREATE TABLE products (
        id INTEGER PRIMARY KEY, name TEXT COLLATE NOCASE, price REAL,
        category TEXT COLLATE NOCASE)""")

    for i in range(filler_tables):
        cols = ", ".join(f"attr_{j} TEXT COLLATE NOCASE" for j in range(12))
        cur.execute(f"CREATE TABLE dim_entity_{i} (id INTEGER PRIMARY KEY, label TEXT, {cols})")

    if rows:
        for i in range(500):
            cur.execute(
                "INSERT INTO customers (name, email, country, city, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (f"Customer {i}", f"c{i}@example.com", "India" if i % 3 else "USA",
                 "Pune" if i % 2 else "Delhi", "active" if i % 5 else "churned", "2026-01-01"),
            )
        for i in range(2000):
            cur.execute(
                "INSERT INTO orders (customer_id, total_amount, status, created_at)"
                " VALUES (?,?,?,?)",
                ((i % 500) + 1, 10.0 + i, "paid" if i % 4 else "refunded", "2026-02-01"),
            )
        for i in range(200):
            cur.execute("INSERT INTO products (name, price, category) VALUES (?,?,?)",
                        (f"Product {i}", 5.0 + i, "widgets" if i % 2 else "gadgets"))
    conn.commit()


def make_db(filler_tables: int, tag: str, *, rows: bool = True) -> str:
    """Build a fresh on-disk SQLite DB and return its path.

    On disk rather than in-memory because the engine's pool hands connections
    between threads; a shared in-memory DB cannot be reopened per connection.
    """
    path = os.path.join(os.environ.get("TEMP", "/tmp"), f"dbbuddy_bench_{tag}.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    build_schema(conn, filler_tables, rows=rows)
    conn.close()
    return path


@contextmanager
def engine_on_sqlite(db_path: str):
    """Point the engine's connection + schema seams at a SQLite file.

    Each ``connect_db`` yields a distinct connection with
    ``check_same_thread=False``: the pool hands connections to whichever thread
    asks next, and the pool — not SQLite — is what guarantees a single owner.
    """
    from tests.test_db_adapter import SqliteDialectConnection, fetch_schema_sqlite

    def open_conn():
        return SqliteDialectConnection(sqlite3.connect(db_path, check_same_thread=False))

    with patch("dbbuddy_core.db.connect_db", side_effect=lambda *a, **k: open_conn()), \
            patch("dbbuddy_core.schema.fetch_schema",
                  side_effect=lambda c: fetch_schema_sqlite(c._conn)):
        yield


def bench_config():
    from dbbuddy_core.models import DBConfig
    return DBConfig(host="localhost", user="bench", password="bench",
                    database=":memory:", ai=False, ai_provider="local")


# -------------------------------------------------------------- baselines ----

BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")


# Key under which provenance lives in baseline.json. Not a benchmark name, so it
# is skipped when the file is read as {benchmark: {metric: ...}}.
BASELINE_META_KEY = "_recorded_on"


def environment_fingerprint() -> dict:
    """Where and on what a baseline was recorded.

    These numbers are wall-clock timings on whatever machine ran them, so a
    baseline is only meaningful against comparable hardware. Recording the
    environment is what lets `--check` say "this baseline is from another machine"
    instead of reporting six confident regressions that are really a slower
    laptop. Without it the only way to tell the difference is to stash your
    changes and re-run — which is exactly the step people skip.
    """
    return {
        "host": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def baseline_environment(path: str = BASELINE_PATH) -> dict:
    """The fingerprint recorded with a baseline, or {} for a pre-provenance file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    meta = (raw or {}).get(BASELINE_META_KEY)
    return meta if isinstance(meta, dict) else {}


def baseline_environment_mismatch(path: str = BASELINE_PATH) -> str | None:
    """A human-readable warning when the baseline came from another machine.

    Returns None when it matches, or when the baseline predates provenance (in
    which case we cannot tell — reported separately by the caller).
    """
    recorded = baseline_environment(path)
    if not recorded:
        return None
    current = environment_fingerprint()
    differing = [
        k for k in ("host", "platform", "processor", "cpu_count")
        if recorded.get(k) != current.get(k)
    ]
    if not differing:
        return None
    return (
        f"baseline was recorded on {recorded.get('host', '?')} "
        f"({recorded.get('platform', '?')}, {recorded.get('cpu_count', '?')} CPUs, "
        f"python {recorded.get('python', '?')}) at {recorded.get('recorded_at', '?')}; "
        f"this run is on {current['host']} ({current['platform']}, "
        f"{current['cpu_count']} CPUs, python {current['python']}). "
        f"Differing: {', '.join(differing)}."
    )


def load_baseline(path: str = BASELINE_PATH) -> dict:
    """Read baseline.json as ``{benchmark: {metric: {value, unit, kind, ...}}}``.

    Accepts the older ``{benchmark: {metric: value}}`` shape too, normalizing it
    on the way in — an out-of-date local baseline should not crash the run, it
    should just carry no classification.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    out: dict = {}
    for bench, metrics in (raw or {}).items():
        if bench == BASELINE_META_KEY:
            continue  # provenance, not a benchmark
        if not isinstance(metrics, dict):
            continue
        out[bench] = {}
        for name, entry in metrics.items():
            if isinstance(entry, dict):
                out[bench][name] = entry
            else:  # legacy: bare number, no recorded classification
                out[bench][name] = {"value": entry, "unit": "", "kind": None,
                                    "lower_is_better": True}
    return out


def save_baseline(results: list[BenchResult], path: str = BASELINE_PATH) -> None:
    """Record values *with* their classification.

    Storing the kind alongside the value is the point: a reader (CI config, a
    dashboard, a human) can answer "which of these fail the build?" from the file
    alone, without importing the benchmarks or re-running them.
    """
    payload = {
        r.benchmark: {
            m.name: {"value": m.value, "unit": m.unit, "kind": m.kind.value,
                     "lower_is_better": m.lower_is_better}
            for m in r.metrics if m.recordable
        }
        for r in results
    }
    # Stamp the machine. See environment_fingerprint().
    payload[BASELINE_META_KEY] = environment_fingerprint()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


@dataclass
class Drift:
    """One metric that moved past tolerance, and whether that blocks."""

    benchmark: str
    metric: Metric
    baseline: float
    ratio: float

    @property
    def blocking(self) -> bool:
        return self.metric.kind is MetricKind.GATE

    def describe(self) -> str:
        direction = "slower" if self.metric.lower_is_better else "lower"
        return (f"{self.benchmark}.{self.metric.name}: {self.metric.value:.3f} "
                f"{self.metric.unit} vs baseline {self.baseline:.3f} "
                f"({self.ratio:.2f}x {direction})")


def compare_to_baseline(results: list[BenchResult], baseline: dict,
                        tolerance: float) -> list[Drift]:
    """Every comparable metric that drifted past tolerance, gating or not.

    Classification decides what a caller *does* with each entry — it does not
    decide what gets reported. A trend metric sliding 3x is worth printing even
    though it must not fail the build.

    A metric missing from the baseline is not a drift: a new benchmark should not
    break the gate on the commit that adds it.
    """
    drifts: list[Drift] = []
    for r in results:
        stored = baseline.get(r.benchmark, {})
        for m in r.metrics:
            entry = stored.get(m.name)
            if entry is None or not m.comparable:
                continue
            base = entry.get("value", 0.0)
            if m.exceeded(base, tolerance):
                drifts.append(Drift(r.benchmark, m, base, m.ratio(base)))
    return drifts


def standalone(result_fn: Callable[..., BenchResult], **kwargs) -> int:
    """Run one benchmark directly and print it. Used by each file's __main__."""
    result = result_fn(**kwargs)
    result.print()
    return 0


def scratch_cache_dir(name: str) -> Optional[str]:
    """Isolated Chroma persist dir, so one benchmark cannot warm another's."""
    return os.path.join(os.environ.get("TEMP", "/tmp"), f"dbbuddy_bench_chroma_{name}")
