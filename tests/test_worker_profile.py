"""What is shared across workers, and what is not, stated at startup.

Several things in this runtime are per worker process, and which ones depends on
whether Redis is reachable. An operator who runs `--workers 4` without Redis gets
four schedulers, four independent ERP ceilings and four divergent prepared
contexts — and finds out from behaviour, not from the process telling them.

So the profile is resolved once and logged. It never refuses to boot: a container
that will not start at 3 a.m. because a cache is down is worse than one that
starts and says what it cannot guarantee.

The worker count has to be *declared* because a worker cannot see its siblings —
uvicorn does not tell a child how many others there are. `LOGIN_GUARD_WORKERS`
already existed for exactly this reason and keeps working as an alias.
"""

import pathlib
import sys

_BACKEND = str(pathlib.Path(__file__).resolve().parents[1] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app_db import profile  # noqa: E402


# ── Resolving the worker count ────────────────────────────────────────────────

def test_defaults_to_one_worker(monkeypatch):
    monkeypatch.delenv("DBBUDDY_WORKERS", raising=False)
    monkeypatch.delenv("LOGIN_GUARD_WORKERS", raising=False)
    assert profile.worker_count() == 1


def test_reads_dbbuddy_workers(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "4")
    assert profile.worker_count() == 4


def test_login_guard_workers_still_works_as_an_alias(monkeypatch):
    # It shipped first and is in existing deployments' env files. Breaking it
    # would silently restore the degraded login-throttle maths it was added for.
    monkeypatch.delenv("DBBUDDY_WORKERS", raising=False)
    monkeypatch.setenv("LOGIN_GUARD_WORKERS", "3")
    assert profile.worker_count() == 3


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "5")
    monkeypatch.setenv("LOGIN_GUARD_WORKERS", "2")
    assert profile.worker_count() == 5


def test_a_nonsense_value_falls_back_to_one(monkeypatch):
    # Refusing to start over a malformed env var would be the hard-fail this
    # deliberately avoids; assuming a large number would under-count every limit.
    monkeypatch.setenv("DBBUDDY_WORKERS", "banana")
    assert profile.worker_count() == 1


def test_zero_and_negative_are_treated_as_one(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "0")
    assert profile.worker_count() == 1
    monkeypatch.setenv("DBBUDDY_WORKERS", "-2")
    assert profile.worker_count() == 1


# ── The resolved profile ──────────────────────────────────────────────────────

def test_a_single_worker_is_never_degraded(monkeypatch):
    # With one process there is nothing to coordinate, so no amount of missing
    # Redis makes the profile wrong.
    monkeypatch.setenv("DBBUDDY_WORKERS", "1")
    monkeypatch.setattr(profile, "_redis_available", lambda: False)
    assert profile.resolve()["degraded"] is False


def test_multi_worker_without_redis_is_degraded(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "2")
    monkeypatch.setattr(profile, "_redis_available", lambda: False)
    assert profile.resolve()["degraded"] is True


def test_multi_worker_with_redis_is_not_degraded(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "2")
    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    assert profile.resolve()["degraded"] is False


def test_the_profile_names_each_subsystem(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "2")
    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    subsystems = profile.resolve()["subsystems"]
    assert {"erp_concurrency", "session_revocation", "prepared_contexts",
            "login_throttle", "scheduler"} <= set(subsystems)


def test_prepared_contexts_are_per_worker_even_with_redis(monkeypatch):
    # The honest bit. A live pool and a Chroma handle cannot be shared, so this
    # never reads "shared" — only its invalidation crosses workers.
    monkeypatch.setenv("DBBUDDY_WORKERS", "2")
    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    assert profile.resolve()["subsystems"]["prepared_contexts"] != "shared"


def test_the_erp_ceiling_reports_shared_only_with_redis(monkeypatch):
    monkeypatch.setenv("DBBUDDY_WORKERS", "2")
    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    assert profile.resolve()["subsystems"]["erp_concurrency"] == "shared"
    monkeypatch.setattr(profile, "_redis_available", lambda: False)
    assert profile.resolve()["subsystems"]["erp_concurrency"] != "shared"


def test_the_effective_erp_ceiling_is_reported(monkeypatch):
    # The number an operator actually needs: without Redis the configured limit
    # applies per worker, so four workers means four times the load on a
    # customer's database.
    monkeypatch.setenv("DBBUDDY_WORKERS", "4")
    monkeypatch.setattr(profile, "_redis_available", lambda: False)
    resolved = profile.resolve()
    assert resolved["effective_erp_limit"] == resolved["erp_limit"] * 4

    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    resolved = profile.resolve()
    assert resolved["effective_erp_limit"] == resolved["erp_limit"]


# ── Logging ───────────────────────────────────────────────────────────────────

def test_a_degraded_profile_is_logged_as_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("DBBUDDY_WORKERS", "3")
    monkeypatch.setattr(profile, "_redis_available", lambda: False)
    with caplog.at_level("WARNING"):
        profile.log_profile()
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_a_healthy_profile_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("DBBUDDY_WORKERS", "1")
    monkeypatch.setattr(profile, "_redis_available", lambda: True)
    with caplog.at_level("WARNING"):
        profile.log_profile()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_logging_never_raises(monkeypatch):
    # It runs inside the lifespan startup. A profile report that can fail a boot
    # is a worse outcome than one that is missing.
    def explode():
        raise RuntimeError("no")

    monkeypatch.setattr(profile, "_redis_available", explode)
    profile.log_profile()
