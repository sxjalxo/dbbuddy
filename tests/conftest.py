"""Shared pytest fixtures + global-state isolation.

The query pipeline caches a prepared per-database context (connection pool,
schema, semantic layer, vector index) in a process-level singleton for speed.
That global state must not leak between tests, or one test's cached context (and
its mocked connection) would be reused by the next. The fixtures below clear it —
and every other piece of process- or Redis-global runtime state — around every
test.

### On skipping tests

The legacy string-based / LLM SQL-generation stack (``generate_sql`` and the
per-provider generators, ``compile_sql_from_intent``, ``is_valid_sql`` /
``clean_sql_output``, and the ``fix_sql`` repair helpers) has been removed — SQL
is compiled deterministically through the planner → Predicate AST → dialect-aware
compiler, and AI does semantic enrichment only. Its tests were deleted with it.

There used to be a second, softer mechanism here: a hardcoded list of seven tests
skipped as "superseded early-stage contracts". On 2026-07-20 all seven were
re-examined and **none of them were drift**. They were hiding three live defects:
joins silently dropped by ``_extract_identifiers`` (so join validation did
nothing on the commonest query shape), table aliases unresolved in join
conditions, and a plan with ``{"type": "INNER"}`` compiling to ``INNER customers
ON …`` — invalid SQL on every engine. Two more were bad hypothesis strategies
generating inputs the property was never about.

The list is gone deliberately. A failing test is a claim that something is wrong;
retiring one requires showing the *product* is right, not filing the test away.
The only skips left are environmental: the real-database "NoMocks" tests, which
need a live MySQL/PostgreSQL and run with ``DBBUDDY_LIVE_DB_TESTS=1``.
"""

import os

import pytest

from dbbuddy_core import context_store

# Tests that need a live database (no mocks).
_LIVE_DB_FILES = {"test_e2e_real_execution.py"}


def pytest_collection_modifyitems(config, items):
    live_ok = os.getenv("DBBUDDY_LIVE_DB_TESTS") == "1"
    if live_ok:
        return
    needs_db = pytest.mark.skip(reason="needs a live database; set DBBUDDY_LIVE_DB_TESTS=1 to run")
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if any(f in nodeid for f in _LIVE_DB_FILES):
            item.add_marker(needs_db)


@pytest.fixture(autouse=True)
def _reset_context_store():
    from dbbuddy_core import ai_metrics, ai_providers
    context_store.reset()
    ai_metrics.reset()          # provider-call metrics are process-global too
    ai_providers.reset_breaker()  # …as is the circuit-breaker state
    yield
    context_store.reset()
    ai_metrics.reset()
    ai_providers.reset_breaker()


@pytest.fixture(autouse=True)
def _reset_shared_runtime_state():
    """Clear process- and Redis-global state the API layer keeps between requests.

    Two things leak across tests otherwise:

    * **The chart result cache.** It is keyed on (org, target, SQL) — deliberately
      not on chart id — and lives in Redis, which *survives the process*. Every
      dashboard test builds its charts from the same fixture org, connection and
      SQL, so they all collide on one key: on a machine with Redis running, a
      later test reads an earlier test's rows and never reaches the code it means
      to exercise. That is why the backpressure test passed alone and failed in a
      full run.
    * **The auth revocation cache**, which would otherwise hold a stale
      (token_version, is_active) for a user id a later test reuses.
    """
    try:
        from app_db import deps
        from app_db.chart_runtime import _CACHE_PREFIX, _get_cache
    except ModuleNotFoundError:
        yield
        return

    def _clear():
        deps.reset_revocation_cache()
        cache = _get_cache()
        if cache is not None:
            try:
                cache.clear_prefix(_CACHE_PREFIX)
            except Exception:
                pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_erp_backpressure():
    """Per-target concurrency semaphores are process-global; a test that leaks a
    slot would silently shrink the ceiling for every later test."""
    from dbbuddy_core import erp_concurrency

    erp_concurrency.reset()
    yield
    erp_concurrency.reset()


@pytest.fixture(autouse=True)
def _reset_login_guard():
    # The login/registration throttle keeps process-global in-memory state; clear
    # it around every test so one test's attempts (many suites register several
    # users from the same TestClient IP) don't leak into the next as a lockout.
    # ``app_db`` lives under backend/ and is only importable once a platform test
    # has put it on sys.path — engine-only test files run without it, so this is a
    # best-effort no-op in that case.
    try:
        from app_db import login_guard
    except ModuleNotFoundError:
        yield
        return

    login_guard.clear_all()
    yield
    login_guard.clear_all()
