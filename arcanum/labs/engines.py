"""B+tree versus LSM tree: the same workload on both engines, measured by the I/O each one does.

Both run on a counting in-memory file system, so every byte written, read and
synced is counted exactly, and both get the same memory: the B+tree as buffer
pool pages, the LSM tree as its memtable and an equal block cache. The B+tree is
Arcanum's own (pager, write-ahead log and checkpoints included).

The three amplifications are the classic way to compare storage engines:
write amplification is bytes written to storage per byte of data written,
read amplification is blocks read per lookup, and space amplification is bytes
on storage per byte of live data.
"""

import random
import time

from ..storage import PAGE_SIZE, BTree, Pager, decode, encode
from ..storage.files import CountingMemFileSystem
from ..storage.lsm import LSMTree

ORDERS = ("sequential", "random")
BATCH = 64  # operations per commit, for both engines
SERIES_POINTS = 120  # samples of the bytes-written curves
SHAPES = 240  # LSM shapes kept for the timeline


def compare(keys=5000, order="random", updates=0.5, value_bytes=64, memory_pages=16, lookups=1000, seed=1):
    """Run the workload on both engines; returns their measurements and the LSM's shape over time."""
    rng = random.Random(seed)
    inserts = list(range(keys))
    if order == "random":
        rng.shuffle(inserts)
    ops = inserts + [rng.randrange(keys) for _ in range(int(keys * updates))]

    def value(i):
        return (f"{i:08d}" * (value_bytes // 8 + 1))[:value_bytes].encode()

    logical = sum(len(encode((k,))) + value_bytes for k in ops)
    live = keys * (len(encode((keys,))) + value_bytes)

    # ---- the B+tree ------------------------------------------------------------------------
    bfs = CountingMemFileSystem()
    pager = Pager("lab/btree.db", pool_size=memory_pages, fs=bfs)
    tree = BTree.create(pager)
    # ---- the LSM tree ----------------------------------------------------------------------
    lfs = CountingMemFileSystem()
    shapes = []
    lsm = None

    def changed(kind, detail):
        shapes.append({"op": len(done), "kind": kind, "detail": detail, **_shape(lsm),
                       "written": lfs.io["write_bytes"]})

    done = []
    lsm = LSMTree(lfs, "lab", memtable_bytes=memory_pages * PAGE_SIZE, block_size=PAGE_SIZE,
                  cache_blocks=memory_pages, on_change=changed)

    series, btree_ms, lsm_ms = [], 0.0, 0.0
    step = max(1, len(ops) // (SERIES_POINTS * BATCH)) * BATCH
    for start in range(0, len(ops), BATCH):
        batch = ops[start:start + BATCH]
        t = time.perf_counter()
        for i, k in enumerate(batch, start):
            tree.insert(encode((k,)), value(i), replace=True)
        pager.commit()
        btree_ms += time.perf_counter() - t
        t = time.perf_counter()
        for i, k in enumerate(batch, start):
            lsm.put(encode((k,)), value(i))
            done.append(1)
        lsm.commit()
        lsm_ms += time.perf_counter() - t
        if start % step == 0 or start + BATCH >= len(ops):
            series.append([start + len(batch), bfs.io["write_bytes"], lfs.io["write_bytes"]])
    # Settle: the B+tree copies its log into the data file, the LSM tree flushes its memtable.
    t = time.perf_counter()
    pager.checkpoint()
    btree_ms += time.perf_counter() - t
    t = time.perf_counter()
    lsm.flush()
    lsm_ms += time.perf_counter() - t
    series.append([len(ops), bfs.io["write_bytes"], lfs.io["write_bytes"]])
    written = {"btree": dict(bfs.io), "lsm": dict(lfs.io)}

    # ---- reads: the same lookups (a fifth of them for keys that do not exist) and range scans ---
    probes = [rng.randrange(keys) if rng.random() < 0.8 else keys + rng.randrange(keys) for _ in range(lookups)]
    scans = [rng.randrange(max(1, keys - 100)) for _ in range(max(1, lookups // 50))]

    def reads(fs, get, scan):
        before = fs.io["reads"]
        t = time.perf_counter()
        for k in probes:
            get(encode((k,)))
        point = fs.io["reads"] - before
        for k in scans:
            scan(encode((k,)), encode((k + 99,)))
        return {"lookup_reads": round(point / len(probes), 2), "scan_reads": round((fs.io["reads"] - before - point)
                / len(scans), 2), "ms": round((time.perf_counter() - t) * 1000, 1)}

    btree_reads = reads(bfs, tree.get, lambda lo, hi: list(tree.scan(lo, hi, hi_inclusive=True)))
    bloom_before = lsm.stats["bloom_skips"]
    lsm_reads = reads(lfs, lsm.get, lsm.scan)

    def result(name, fs, ms, read, extra):
        io = written[name]
        return {"write_amp": round(io.get("write_bytes", 0) / logical, 2),
                "log_bytes": io.get("log_write_bytes", 0), "data_bytes": io.get("data_write_bytes", 0),
                "syncs": io.get("syncs", 0), "write_ms": round(ms * 1000, 1),
                "space_amp": round(fs.size() / live, 2), "disk_bytes": fs.size(), **read, **extra}

    height, node = 1, tree._load(tree.root)
    while not node.leaf:
        node, height = tree._load(node.children[0]), height + 1
    return {
        "workload": {"keys": keys, "order": order, "updates": updates, "value_bytes": value_bytes,
                     "memory_pages": memory_pages, "operations": len(ops), "logical_bytes": logical,
                     "live_bytes": live, "lookups": lookups, "scans": len(scans), "batch": BATCH},
        "btree": result("btree", bfs, btree_ms, btree_reads, {"height": height, "pages": pager.page_count,
                                                              "checkpoints": pager.stats["checkpoints"]}),
        "lsm": result("lsm", lfs, lsm_ms, lsm_reads, {
            "flushes": lsm.stats["flushes"], "compactions": lsm.stats["compactions"], "moves": lsm.stats["moves"],
            "bloom_skips": lsm.stats["bloom_skips"] - bloom_before, "filter_memory": lsm.memory(),
            "levels": len(lsm.levels)}),
        "series": series,
        "shapes": _thin(shapes, SHAPES),
        "final": {**_shape(lsm), "written": lfs.io["write_bytes"]},
        "key_space": keys,
    }


def _shape(lsm):
    shape = lsm.shape()
    for tables in shape["levels"]:
        for t in tables:
            t["first"], t["last"] = decode(t["first"])[0], decode(t["last"])[0]
    return shape


def _thin(items, limit):
    if len(items) <= limit:
        return items
    return [items[round(i * (len(items) - 1) / (limit - 1))] for i in range(limit)]
