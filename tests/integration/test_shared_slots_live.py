"""The shared ERP ceiling against a real Redis, across separate limiter instances.

`tests/test_erp_shared_slots.py` drives the admission algorithm through a stub,
which is what makes it checkable on a machine with no Redis. This is the other
half: that real Redis behaves the way the stub claims, and that two instances
which share nothing but the server still share one ceiling.

Two instances in one process stand in for two workers. That is the property under
test — the limiter holds no cross-instance state, so two objects here and two
processes on two machines are the same case as far as the algorithm is concerned.

Opt-in like the rest of `tests/integration/`:

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration
"""

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

redis = pytest.importorskip("redis", reason="redis client not installed")

from dbbuddy_core.erp_concurrency import _SharedSlots  # noqa: E402


@pytest.fixture
def client():
    url = os.getenv("REDIS_URL")
    if url:
        conn = redis.Redis.from_url(url, socket_connect_timeout=2)
    else:
        conn = redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            socket_connect_timeout=2,
        )
    try:
        conn.ping()
    except Exception as exc:                    # noqa: BLE001
        pytest.skip(f"no Redis available ({exc})")
    return conn


@pytest.fixture
def target(client):
    """A key nothing else will touch, cleaned up afterwards."""
    name = f"live-test-{uuid.uuid4().hex}"
    yield name
    client.delete(f"dbbuddy:erp:slots:{name}")


def test_two_instances_share_one_ceiling(client, target):
    # The whole point. Two workers, one limit — not one limit each.
    worker_a = _SharedSlots(client, limit=2, lease=60.0)
    worker_b = _SharedSlots(client, limit=2, lease=60.0)

    assert worker_a.acquire(target, "a1") is True
    assert worker_b.acquire(target, "b1") is True
    assert worker_a.acquire(target, "a2") is False
    assert worker_b.acquire(target, "b2") is False
    assert worker_a.occupancy(target) == 2


def test_a_release_on_one_instance_frees_a_slot_on_the_other(client, target):
    worker_a = _SharedSlots(client, limit=1, lease=60.0)
    worker_b = _SharedSlots(client, limit=1, lease=60.0)

    assert worker_a.acquire(target, "a1") is True
    assert worker_b.acquire(target, "b1") is False
    worker_a.release(target, "a1")
    assert worker_b.acquire(target, "b1") is True


def test_an_expired_lease_is_reclaimed(client, target):
    # A worker that died mid-query. Without pruning its slot is gone for good and
    # the ceiling for that target quietly shrinks.
    slots = _SharedSlots(client, limit=1, lease=60.0)
    client.zadd(f"dbbuddy:erp:slots:{target}", {"crashed-worker": time.time() - 1})
    assert slots.acquire(target, "fresh") is True
    assert slots.occupancy(target) == 1


def test_a_refused_acquire_leaves_no_trace(client, target):
    slots = _SharedSlots(client, limit=1, lease=60.0)
    slots.acquire(target, "holder")
    slots.acquire(target, "refused")
    assert client.zscore(f"dbbuddy:erp:slots:{target}", "refused") is None


def test_the_key_expires_so_idle_targets_do_not_accumulate(client, target):
    # One key per target forever would be a slow leak on an instance that has seen
    # many databases.
    slots = _SharedSlots(client, limit=1, lease=60.0)
    slots.acquire(target, "t1")
    assert client.pttl(f"dbbuddy:erp:slots:{target}") > 0


def test_equal_scores_do_not_admit_past_the_limit(client, target):
    # The defect that ruled out rank-based admission, against the real server:
    # time.time() has ~15 ms resolution on Windows, so a burst of acquirers shares
    # one score and Redis breaks the tie by member — a random token. Ranking let a
    # latecomer sorting first slip in while the current holders kept their slots.
    key = f"dbbuddy:erp:slots:{target}"
    same_score = time.time() + 60.0
    client.zadd(key, {"mmm": same_score, "nnn": same_score})
    slots = _SharedSlots(client, limit=2, lease=60.0)
    assert slots.acquire(target, "aaa") is False
    assert slots.occupancy(target) == 2
