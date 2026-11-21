"""Buffer pool replacement policies: correct as pagers, and behaving as their algorithms say."""

import random

import pytest

from arcanum import Database
from arcanum.labs.buffer_pool import OPTIMAL, compare, replay
from arcanum.storage.policies import POLICIES, make_policy


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_every_policy_gives_the_same_database(tmp_path, policy):
    rng = random.Random(5)
    with Database(tmp_path / f"{policy}.db", pool_size=6, max_keys=4, pool_policy=policy) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("CREATE INDEX t_v ON t (v)")
        expected = {}
        for i in range(600):
            k = rng.randrange(200)
            if rng.random() < 0.7:
                db.execute(f"INSERT INTO t VALUES ({k}, 'v{i}') ON CONFLICT (id) DO UPDATE SET v = excluded.v")
                expected[k] = f"v{i}"
            else:
                db.execute(f"DELETE FROM t WHERE id = {k}")
                expected.pop(k, None)
        assert dict(db.execute("SELECT id, v FROM t").rows) == expected
        assert db.pager.stats["evictions"] > 100  # the tiny pool really was under pressure
        assert len(db.pager.cached_pages()) <= 6
    with Database(tmp_path / f"{policy}.db", pool_policy=policy) as db:
        assert dict(db.execute("SELECT id, v FROM t").rows) == expected


def test_lru_and_clock_on_a_known_trace():
    trace = [1, 2, 3, 1, 4, 1, 5, 2]
    # LRU, 3 pages: hits on the second 1 and the third 1 only.
    assert replay("lru", trace, 3) == [False, False, False, True, False, True, False, False]
    clock = make_policy("clock", 3)
    for page in (1, 2, 3):
        clock.touch(page)
    assert clock.victim() == 1  # every bit was set: a full sweep clears them, then the hand's first page goes


def test_two_q_resists_a_scan():
    # Hot pages mixed with pages used once: 2Q sees the hot ones come back and promotes them to its main queue.
    warm = [p for i in range(60) for p in (1, 2, 3, 4, 1000 + i)]
    hot = [1, 2, 3, 4] * 20
    scan = list(range(100, 200))
    trace = warm + scan + hot
    after = len(warm) + len(scan)
    lru, two_q = replay("lru", trace, 12)[after:], replay("2q", trace, 12)[after:]
    # The scan went through 2Q's FIFO and left the hot pages in its main queue; it flushed LRU's pool.
    assert all(two_q) and not all(lru)


@pytest.mark.parametrize("seed", range(5))
def test_nothing_beats_the_optimal_policy(seed):
    rng = random.Random(seed)
    trace = [int(rng.paretovariate(1.2)) % 60 for _ in range(3000)]
    for size in (4, 16, 40):
        best = sum(replay(OPTIMAL, trace, size))
        for name in POLICIES:
            assert sum(replay(name, trace, size)) <= best


def test_the_lab_reports_every_policy():
    lab = compare("loop", pool_pages=16, rows=600, lookups=40)
    assert lab["policies"]["lru"]["hit_rate"] == 0  # a loop larger than the pool defeats LRU entirely
    assert lab["policies"][OPTIMAL]["hit_rate"] > 0
    assert len(lab["sweep"]["sizes"]) == len(lab["sweep"]["hit_rates"]["lru"])
