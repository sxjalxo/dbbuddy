"""Backpressure and statement timeouts — protecting the *customer's* database.

Pool sizes and parallelism limits protect DB Buddy from running out of
connections. These two mechanisms protect the ERP on the other end: a ceiling on
how many queries DB Buddy will run against one target at once, and a ceiling on
how long any single statement may take.
"""

import threading
import time

import pytest

from dbbuddy_core import erp_concurrency
from dbbuddy_core.dialects import get_dialect


@pytest.fixture(autouse=True)
def _reset():
    erp_concurrency.reset()
    yield
    erp_concurrency.reset()


# ── Target identity ──────────────────────────────────────────────────────────

def test_targets_are_isolated_from_each_other():
    a = erp_concurrency.target_key("mysql", "host-a", 3306, "sales")
    b = erp_concurrency.target_key("mysql", "host-b", 3306, "sales")
    assert a != b


def test_username_is_not_part_of_the_target():
    # Two analysts with separate credentials still contend for the same machine,
    # and it is the machine being protected.
    assert erp_concurrency.target_key("mysql", "h", 3306, "sales") == \
        erp_concurrency.target_key("MySQL", "H", 3306, "Sales")


# ── The ceiling ──────────────────────────────────────────────────────────────

def test_concurrency_is_capped_per_target(monkeypatch):
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 3)
    erp_concurrency.reset()

    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()
    release = threading.Event()

    def worker():
        with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=5):
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            release.wait(timeout=5)
            with lock:
                peak["cur"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.3)  # let everything that can hold a slot take one
    observed = peak["n"]
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert observed <= 3, f"{observed} concurrent queries exceeded the ceiling of 3"


def test_a_saturated_target_gives_up_rather_than_waiting_forever(monkeypatch):
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 1)
    erp_concurrency.reset()

    started = threading.Event()
    release = threading.Event()

    def hold():
        with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=5):
            started.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    started.wait(timeout=5)
    try:
        with pytest.raises(erp_concurrency.ERPBusy):
            with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=0.2):
                pass
    finally:
        release.set()
        t.join(timeout=5)


def test_one_busy_target_does_not_starve_another(monkeypatch):
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 1)
    erp_concurrency.reset()

    release = threading.Event()
    started = threading.Event()

    def hold():
        with erp_concurrency.query_slot("mysql", "busy", 3306, "db", timeout=5):
            started.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    started.wait(timeout=5)
    try:
        # A different target is unaffected — the semaphores are independent.
        with erp_concurrency.query_slot("mysql", "quiet", 3306, "db", timeout=0.5):
            pass
    finally:
        release.set()
        t.join(timeout=5)


def test_a_slot_is_released_even_when_the_body_raises(monkeypatch):
    # A leaked slot permanently shrinks the ceiling — a slow, hard-to-diagnose
    # failure, so the release must survive an exception.
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 1)
    erp_concurrency.reset()

    with pytest.raises(ValueError):
        with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=1):
            raise ValueError("boom")

    # Still acquirable, so nothing leaked.
    with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=0.5):
        pass


def test_snapshot_reports_occupancy(monkeypatch):
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 4)
    erp_concurrency.reset()
    with erp_concurrency.query_slot("mysql", "h", 3306, "db", timeout=1):
        snap = erp_concurrency.snapshot()
    key = erp_concurrency.target_key("mysql", "h", 3306, "db")
    assert snap[key]["limit"] == 4
    assert snap[key]["in_use"] == 1


# ── Dashboard parallelism is derived, not independently configured ───────────

def test_dashboard_parallelism_cannot_exceed_the_target_ceiling():
    # These were originally two independent numbers (pool 5 vs parallelism 6), so
    # a dashboard could fan out wider than its target allows and queue against
    # itself — a stall with no visible cause.
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend"))
    from app_db import chart_runtime

    assert chart_runtime.MAX_PARALLEL_QUERIES <= erp_concurrency.MAX_CONCURRENT_PER_TARGET
    assert chart_runtime.MAX_PARALLEL_QUERIES >= 1


# ── Statement timeouts ───────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, sink, fail=False):
        self.sink = sink
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("unknown system variable")
        self.sink.append((sql, params))

    def close(self):
        pass


class _FakeConn:
    def __init__(self, fail=False):
        self.statements = []
        self._fail = fail

    def cursor(self, *a, **k):
        return _FakeCursor(self.statements, self._fail)


@pytest.mark.parametrize("engine,needle", [
    ("mysql", "MAX_EXECUTION_TIME"),
    ("postgresql", "statement_timeout"),
])
def test_dialect_sets_a_statement_timeout_in_milliseconds(engine, needle):
    conn = _FakeConn()
    assert get_dialect(engine).apply_statement_timeout(conn, 30) is True
    sql, params = conn.statements[0]
    assert needle in sql
    assert params == (30_000,)  # seconds → milliseconds


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_an_older_server_degrades_instead_of_failing_the_connection(engine):
    # Applied as a session setting after connect precisely so an unsupported
    # server loses the ceiling rather than losing the connection.
    conn = _FakeConn(fail=True)
    assert get_dialect(engine).apply_statement_timeout(conn, 30) is False


def test_a_dialect_without_support_reports_false_rather_than_pretending():
    # SQL Server has no session-level statement timeout; saying "no" is the
    # honest answer, and callers can tell a real bound from a best-effort one.
    assert get_dialect("sqlserver").apply_statement_timeout(_FakeConn(), 30) is False


def test_connect_db_applies_the_timeout(monkeypatch):
    from dbbuddy_core import db as db_module

    applied = {}

    class _Dialect:
        def connect(self, *a, **k):
            return _FakeConn()

        def apply_statement_timeout(self, conn, seconds):
            applied["seconds"] = seconds
            return True

    monkeypatch.setattr(db_module, "get_dialect", lambda engine: _Dialect())
    monkeypatch.setattr(db_module, "STATEMENT_TIMEOUT_SECONDS", 42)
    assert db_module.connect_db("h", "u", "p", "d") is not None
    assert applied["seconds"] == 42


def test_a_zero_timeout_disables_the_ceiling(monkeypatch):
    from dbbuddy_core import db as db_module

    applied = {"called": False}

    class _Dialect:
        def connect(self, *a, **k):
            return _FakeConn()

        def apply_statement_timeout(self, conn, seconds):
            applied["called"] = True
            return True

    monkeypatch.setattr(db_module, "get_dialect", lambda engine: _Dialect())
    monkeypatch.setattr(db_module, "STATEMENT_TIMEOUT_SECONDS", 0)
    db_module.connect_db("h", "u", "p", "d")
    assert applied["called"] is False
