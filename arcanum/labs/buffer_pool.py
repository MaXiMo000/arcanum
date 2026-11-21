"""Buffer pool replacement policies compared on the same page-access trace.

A workload of real queries runs on a fresh in-memory database whose pool is big
enough to hold everything, while every page the engine asks for is recorded: the
page-access trace. That trace is then replayed through each policy the pager
can use (see storage/policies.py) at the pool size asked for, and at a range of
sizes, counting hits. Belady's optimal policy (evict the page whose next use is
furthest away) is replayed too: no real policy can do better, so it shows how
much room each one leaves.
"""

import heapq
import random
from bisect import bisect_left
from itertools import accumulate

from ..engine import Database
from ..storage.files import CountingMemFileSystem
from ..storage.policies import POLICIES, make_policy

WORKLOADS = {
    "hot": "Point lookups where a few rows are far more popular than the rest (Zipf-distributed keys).",
    "hot-scan": "The same lookups, with a full table scan every 150 lookups: the scan floods an LRU pool.",
    "loop": "The same table scanned again and again: LRU and Clock evict each page just before it is needed.",
    "index": "Lookups through a secondary index and then the table: two trees compete for the pool.",
}
OPTIMAL = "optimal"
SERIES_POINTS = 100
SWEEP = (4, 8, 16, 32, 64, 128, 256, 512)
ZIPF_S = 1.1


def compare(workload="hot-scan", pool_pages=32, rows=4000, lookups=1500, seed=7):
    """Record the workload's page accesses and replay them through every policy."""
    trace, table_pages = record(workload, rows, lookups, seed)
    distinct = len(set(trace))
    policies = {}
    for name in [*POLICIES, OPTIMAL]:
        hits = replay(name, trace, pool_pages)
        policies[name] = {"hits": sum(hits), "misses": len(hits) - sum(hits),
                          "hit_rate": round(sum(hits) / len(hits), 4) if hits else 0.0, "series": _series(hits)}
    sizes = sorted({s for s in (*SWEEP, pool_pages) if s < distinct} | {distinct})
    sweep = {name: [round(sum(replay(name, trace, size)) / len(trace), 4) for size in sizes]
             for name in [*POLICIES, OPTIMAL]}
    return {"workload": {"name": workload, "about": WORKLOADS[workload], "pool_pages": pool_pages, "rows": rows,
                         "lookups": lookups, "accesses": len(trace), "distinct_pages": distinct,
                         "table_pages": table_pages},
            "policies": policies, "sweep": {"sizes": sizes, "hit_rates": sweep}}


def record(workload, rows, lookups, seed):
    """The pages the engine reads, in order, while the workload runs (and the table's size in pages)."""
    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload {workload!r}; choose one of {', '.join(WORKLOADS)}")
    rng = random.Random(seed)
    db = Database("lab", fs=CountingMemFileSystem(), pool_size=1_000_000)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, pad TEXT)")
    db.execute("CREATE INDEX t_grp ON t (grp)")
    db.execute(f"INSERT INTO t SELECT n, n % {max(rows // 4, 1)}, repeat('p', 60) || n FROM generate_series(1, {rows}) AS n")
    table_pages = len(db.catalog.btree(db.catalog.tables["t"].root).dump())
    trace = []
    db.events.subscribe(lambda kind, data: trace.append(data["page"]) if kind == "page_read" else None)
    weights = list(accumulate(1 / k ** ZIPF_S for k in range(1, rows + 1)))
    order = list(range(1, rows + 1))
    rng.shuffle(order)  # popular keys are spread over the table, not packed into its first pages

    def popular():
        return order[bisect_left(weights, rng.random() * weights[-1])]

    for i in range(lookups):
        if workload == "loop":
            db.execute("SELECT count(*) FROM t WHERE pad <> ''")
            if i >= 30:
                break
            continue
        if workload == "index":
            db.execute(f"SELECT pad FROM t WHERE grp = {popular() % max(rows // 4, 1)}")
        else:
            db.execute(f"SELECT pad FROM t WHERE id = {popular()}")
        if workload == "hot-scan" and i % 150 == 149:
            db.execute("SELECT count(*) FROM t WHERE pad <> ''")
    return trace, table_pages


def replay(name, trace, size):
    """Per access, whether it hit a pool of `size` pages managed by policy `name`."""
    if name == OPTIMAL:
        return _optimal(trace, size)
    policy, cached, hits = make_policy(name, size), set(), []
    for page in trace:
        hit = page in cached
        hits.append(hit)
        policy.touch(page)
        if not hit:
            cached.add(page)
            if len(cached) > size:  # the same order as the pager: admit, then evict
                victim = policy.victim()
                cached.remove(victim)
                policy.remove(victim, evicted=True)
    return hits


def _optimal(trace, size):
    """Belady's policy: on a miss with a full pool, evict the page whose next use is furthest away."""
    never = len(trace)
    following, seen = [never] * len(trace), {}
    for i in range(len(trace) - 1, -1, -1):
        following[i] = seen.get(trace[i], never)
        seen[trace[i]] = i
    cached, heap, hits = {}, [], []  # page -> its next use; heap of (-next use, page), stale entries skipped
    for i, page in enumerate(trace):
        hit = page in cached
        hits.append(hit)
        if not hit and len(cached) >= size:
            while True:
                distance, victim = heapq.heappop(heap)
                if cached.get(victim) == -distance:
                    del cached[victim]
                    break
        cached[page] = following[i]
        heapq.heappush(heap, (-following[i], page))
    return hits


def _series(hits):
    """Hit rate over a sliding window, sampled at SERIES_POINTS points: [accesses so far, hit rate]."""
    if not hits:
        return []
    window = max(1, len(hits) // 20)
    sums = [0, *accumulate(hits)]
    step = max(1, len(hits) // SERIES_POINTS)
    return [[i, round((sums[i] - sums[max(0, i - window)]) / min(window, i), 4)]
            for i in range(step, len(hits) + 1, step)]
