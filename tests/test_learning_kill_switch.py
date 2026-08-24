"""Learning must be switchable off, or nothing about the engine is reproducible.

`update_memory` runs after every successful query, so asking the same question
twice can produce two different plans: the first run teaches the engine something
that changes the second. That is the feature working as designed, and it is also
why a demo is not reproducible between a maintainer's warm instance and a
visitor's cold one — and why bisecting a planner bug by re-running a query gives
answers that drift under you.

`DBBUDDY_DISABLE_LEARNING=1` freezes the semantic memory: reads still apply
whatever has already been learned, writes stop. That makes a run repeatable
without pretending the learned layer does not exist.

Read at call time, not import time, so a test or a container can flip it without
re-importing the module.
"""

import dbbuddy_core.learning_engine as learning_engine


def _intent():
    return {
        "tables": ["payments"],
        "aggregation": {"function": "SUM", "column": {"table": "payments", "column": "amount"}},
        "select": [],
    }


def test_learning_is_on_by_default(monkeypatch):
    monkeypatch.delenv("DBBUDDY_DISABLE_LEARNING", raising=False)
    assert learning_engine.learning_enabled() is True


def test_kill_switch_stops_writes(monkeypatch):
    monkeypatch.setenv("DBBUDDY_DISABLE_LEARNING", "1")
    assert learning_engine.learning_enabled() is False

    called = []
    monkeypatch.setattr(learning_engine, "load_memory", lambda *a, **k: called.append("load") or {})

    learning_engine.update_memory("total revenue", _intent(), {}, success=True, score=1.0)

    assert called == [], "memory was touched with learning disabled"


def test_switch_is_read_at_call_time(monkeypatch):
    """Flipping it must take effect without re-importing the module."""
    monkeypatch.setenv("DBBUDDY_DISABLE_LEARNING", "1")
    assert learning_engine.learning_enabled() is False
    monkeypatch.setenv("DBBUDDY_DISABLE_LEARNING", "0")
    assert learning_engine.learning_enabled() is True


def test_accepts_the_usual_truthy_spellings(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("DBBUDDY_DISABLE_LEARNING", value)
        assert learning_engine.learning_enabled() is False, value
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("DBBUDDY_DISABLE_LEARNING", value)
        assert learning_engine.learning_enabled() is True, value
