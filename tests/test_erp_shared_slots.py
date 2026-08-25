"""The per-target concurrency ceiling, counted across worker processes.

The semaphore protects a customer's ERP from DB Buddy's own popularity. It was a
process-level `BoundedSemaphore`, so N workers meant N × the configured ceiling
against a database that also has a business to run — and `DEPLOYMENT.md` asked the
operator to divide the setting by their worker count to compensate.

Counting now happens in Redis: a sorted set per target whose members are leases.
Admission is by *rank* rather than by count, so two workers racing for the last
slot agree on which of them won instead of both backing out; the scheme can
under-admit for one retry cycle and can never over-admit, which for a ceiling
protecting someone else's database is the correct direction to be wrong in.

These tests drive the algorithm through a stub implementing the four commands it
uses. That is deliberate: there is no Redis service in `docker-compose.test.yml`,
so a Lua-based implementation could only be checked against a live server. The
part that is easy to get wrong is the admission logic, and this is what makes it
checkable with no server at all. The live cross-instance check lives in
`tests/integration/`.
"""

import time

import pytest

from dbbuddy_core import erp_concurrency


class FakeRedis:
    """The four sorted-set commands the limiter uses, and nothing else.

    Faithful where it matters: `zadd` on an existing member updates its score
    rather than adding a duplicate (which is what makes a re-acquire by the same
    token a no-op), and `zrank` orders by score then lexically by member, exactly
    as Redis does.
    """

    def __init__(self):
        self.sets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}
        self.calls: list[str] = []

    # -- pipeline ------------------------------------------------------------
    def pipeline(self, transaction: bool = True):
        return FakePipeline(self)

    # -- commands ------------------------------------------------------------
    def zremrangebyscore(self, key, low, high):
        members = self.sets.setdefault(key, {})
        doomed = [m for m, s in members.items() if low <= s <= high]
        for m in doomed:
            del members[m]
        return len(doomed)

    def zadd(self, key, mapping):
        members = self.sets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in members)
        members.update(mapping)
        return added

    def zrank(self, key, member):
        members = self.sets.get(key, {})
        if member not in members:
            return None
        order = sorted(members.items(), key=lambda kv: (kv[1], kv[0]))
        return [m for m, _ in order].index(member)

    def zrem(self, key, member):
        return 1 if self.sets.setdefault(key, {}).pop(member, None) is not None else 0

    def zcard(self, key):
        return len(self.sets.get(key, {}))

    def pexpire(self, key, ms):
        self.expires[key] = ms
        return 1


class FakePipeline:
    """Queues commands and runs them on execute(), like redis-py's MULTI."""

    def __init__(self, client: FakeRedis):
        self._client = client
        self._queued = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        results = []
        for name, args, kwargs in self._queued:
            self._client.calls.append(name)
            results.append(getattr(self._client, name)(*args, **kwargs))
        self._queued.clear()
        return results


@pytest.fixture
def slots():
    return erp_concurrency._SharedSlots(FakeRedis(), limit=2, lease=60.0)


@pytest.fixture(autouse=True)
def clean():
    erp_concurrency.reset()
    yield
    erp_concurrency.reset()


# ── Admission ─────────────────────────────────────────────────────────────────

def test_a_free_target_admits(slots):
    assert slots.acquire("erp-a", "token-1") is True


def test_admits_up_to_the_limit(slots):
    assert slots.acquire("erp-a", "t1") is True
    assert slots.acquire("erp-a", "t2") is True


def test_refuses_beyond_the_limit(slots):
    slots.acquire("erp-a", "t1")
    slots.acquire("erp-a", "t2")
    assert slots.acquire("erp-a", "t3") is False


def test_a_refused_holder_leaves_nothing_behind(slots):
    # Admission adds optimistically and backs out on a losing rank. A token left
    # in the set would permanently consume a slot — the leak the local
    # implementation guards against with try/finally.
    slots.acquire("erp-a", "t1")
    slots.acquire("erp-a", "t2")
    slots.acquire("erp-a", "t3")
    assert slots.occupancy("erp-a") == 2


def test_release_frees_exactly_one_slot(slots):
    slots.acquire("erp-a", "t1")
    slots.acquire("erp-a", "t2")
    slots.release("erp-a", "t1")
    assert slots.occupancy("erp-a") == 1
    assert slots.acquire("erp-a", "t3") is True


def test_releasing_a_token_that_never_held_is_harmless(slots):
    slots.acquire("erp-a", "t1")
    slots.release("erp-a", "nobody")
    assert slots.occupancy("erp-a") == 1


def test_targets_are_independent(slots):
    slots.acquire("erp-a", "t1")
    slots.acquire("erp-a", "t2")
    # A saturated ERP A must never starve ERP B: separate keys, separate counts.
    assert slots.acquire("erp-b", "t3") is True


# ── Leases ────────────────────────────────────────────────────────────────────

def test_an_expired_lease_is_reclaimed_on_the_next_acquire(slots):
    # A worker that crashed mid-query left its lease behind. Without pruning, the
    # ceiling for that target shrinks permanently.
    slots.acquire("erp-a", "dead-1")
    slots.acquire("erp-a", "dead-2")
    slots._client.sets["dbbuddy:erp:slots:erp-a"] = {
        "dead-1": time.time() - 100,
        "dead-2": time.time() - 100,
    }
    assert slots.acquire("erp-a", "fresh") is True
    assert slots.occupancy("erp-a") == 1


def test_a_live_lease_is_not_reclaimed(slots):
    slots.acquire("erp-a", "live")
    assert slots.occupancy("erp-a") == 1
    assert slots.acquire("erp-a", "second") is True


def test_the_key_is_given_an_expiry_so_idle_targets_evaporate(slots):
    slots.acquire("erp-a", "t1")
    assert slots._client.expires["dbbuddy:erp:slots:erp-a"] > 0


# ── Rank tie-break ────────────────────────────────────────────────────────────

def test_only_one_of_two_workers_gets_the_last_slot(slots):
    one = erp_concurrency._SharedSlots(slots._client, limit=1, lease=60.0)
    two = erp_concurrency._SharedSlots(slots._client, limit=1, lease=60.0)
    results = [one.acquire("erp-a", "aaa"), two.acquire("erp-a", "zzz")]
    assert results.count(True) == 1
    assert one.occupancy("erp-a") == 1


def test_a_late_arrival_that_sorts_first_is_still_refused(slots):
    # The defect that ruled out rank-based admission. Scores are expiries from
    # time.time(), whose resolution on Windows is ~15 ms, so a burst of acquirers
    # shares one score and Redis breaks the tie by member — a random token. Under
    # rank-based admission a latecomer sorting ahead of the current holders got
    # rank 0 and was admitted while they kept their slots, and the ceiling was
    # quietly exceeded. Counting does not depend on ordering.
    key = "dbbuddy:erp:slots:erp-a"
    same_score = time.time() + 60.0
    slots._client.sets[key] = {"mmm": same_score, "nnn": same_score}   # limit is 2
    assert slots.acquire("erp-a", "aaa") is False                      # sorts first
    assert slots.occupancy("erp-a") == 2


def test_the_same_token_acquiring_twice_does_not_consume_two_slots(slots):
    slots.acquire("erp-a", "t1")
    slots.acquire("erp-a", "t1")
    assert slots.occupancy("erp-a") == 1


# ── Lease duration ────────────────────────────────────────────────────────────

def test_the_lease_outlives_the_statement_timeout(monkeypatch):
    # Reclaiming an expired lease is only safe if the query cannot still be
    # running. The statement timeout is what guarantees that, so the lease is
    # derived from it with a margin.
    monkeypatch.setattr(erp_concurrency, "STATEMENT_TIMEOUT_SECONDS", 60.0)
    assert erp_concurrency.lease_seconds() > 60.0


def test_a_disabled_statement_timeout_falls_back_to_a_fixed_lease(monkeypatch):
    # With no statement timeout there is no guarantee at all; a fixed lease is a
    # bound rather than a guarantee, and the setting that opens this hole is
    # documented as such.
    monkeypatch.setattr(erp_concurrency, "STATEMENT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(erp_concurrency, "SLOT_LEASE_SECONDS", 300.0)
    assert erp_concurrency.lease_seconds() == 300.0


def test_the_lease_has_a_floor(monkeypatch):
    # A one-second statement timeout must not produce a lease so short that a
    # slot is reclaimed while its holder is still connecting.
    monkeypatch.setattr(erp_concurrency, "STATEMENT_TIMEOUT_SECONDS", 1.0)
    assert erp_concurrency.lease_seconds() >= 60.0


# ── Choosing a backend ────────────────────────────────────────────────────────

def test_no_redis_means_the_local_semaphore_not_no_limit(monkeypatch):
    # The engine's query limiter fails open; this one must not. Failing open here
    # means unbounded concurrent queries against a customer's production ERP at
    # the moment our own cache is already unhealthy.
    monkeypatch.setattr(erp_concurrency, "_shared_slots", lambda: None)
    with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp"):
        pass
    assert erp_concurrency.backend_name() == "in-process"


def test_the_local_ceiling_still_holds_without_redis(monkeypatch):
    monkeypatch.setattr(erp_concurrency, "_shared_slots", lambda: None)
    monkeypatch.setattr(erp_concurrency, "MAX_CONCURRENT_PER_TARGET", 1)
    erp_concurrency.reset()
    with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp"):
        with pytest.raises(erp_concurrency.ERPBusy):
            with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp",
                                            timeout=0.05):
                pass


def test_a_shared_slot_is_released_even_when_the_body_raises(monkeypatch):
    client = FakeRedis()
    shared = erp_concurrency._SharedSlots(client, limit=1, lease=60.0)
    monkeypatch.setattr(erp_concurrency, "_shared_slots", lambda: shared)

    with pytest.raises(ValueError):
        with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp"):
            raise ValueError("boom")

    key = erp_concurrency.target_key("postgresql", "db.internal", 5432, "erp")
    assert shared.occupancy(key) == 0


def test_the_shared_ceiling_is_enforced_through_query_slot(monkeypatch):
    client = FakeRedis()
    shared = erp_concurrency._SharedSlots(client, limit=1, lease=60.0)
    monkeypatch.setattr(erp_concurrency, "_shared_slots", lambda: shared)

    with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp"):
        with pytest.raises(erp_concurrency.ERPBusy):
            with erp_concurrency.query_slot("postgresql", "db.internal", 5432, "erp",
                                            timeout=0.05):
                pass
