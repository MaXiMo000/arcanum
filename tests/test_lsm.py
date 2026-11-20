"""The LSM tree: correctness against a dict through flushes, compactions and deletions."""

import random
from itertools import pairwise

from arcanum.storage import encode
from arcanum.storage.files import CountingMemFileSystem
from arcanum.storage.lsm import LSMTree


def test_lsm_matches_a_dict_through_compactions():
    fs, rng, model = CountingMemFileSystem(), random.Random(7), {}
    events = []
    lsm = LSMTree(fs, "lab", memtable_bytes=2048, level0_files=3, ratio=3, cache_blocks=4,
                  on_change=lambda kind, detail: events.append(kind))
    for i in range(6000):
        key = encode((rng.randrange(1500),))
        if rng.random() < 0.15:
            lsm.delete(key)
            model.pop(key, None)
        else:
            value = f"v{i}".encode() * rng.randrange(1, 6)
            lsm.put(key, value)
            model[key] = value
        if i % 50 == 0:
            lsm.commit()
    assert "flush" in events and "compaction" in events
    assert len(lsm.levels) >= 3  # data reached deeper levels
    for level in lsm.levels[1:]:  # deeper levels are sorted runs of non-overlapping files
        assert all(a.last < b.first for a, b in pairwise(level))
    for n in range(1600):
        key = encode((n,))
        assert lsm.get(key) == model.get(key)
    lo, hi = encode((200,)), encode((400,))
    assert lsm.scan(lo, hi) == sorted((k, v) for k, v in model.items() if lo <= k <= hi)
    lsm.flush()
    assert lsm.memtable == {} and fs.files["lab/lsm-wal"] == bytearray()
    assert lsm.stats["bloom_skips"] > 0


def test_counting_file_system_counts_bytes_by_kind():
    fs = CountingMemFileSystem()
    log, data = fs.open("x-wal"), fs.open("x.db")
    log.write(0, b"abc")
    data.write(10, b"de")
    log.sync()
    assert data.read(0, 12) == bytes(10) + b"de"
    assert (fs.io["log_write_bytes"], fs.io["data_write_bytes"], fs.io["syncs"], fs.io["read_bytes"]) == (3, 2, 1, 12)


def test_sequential_keys_move_files_down_without_rewriting_them():
    fs = CountingMemFileSystem()
    lsm = LSMTree(fs, "lab", memtable_bytes=1024, level0_files=2, ratio=2)
    for n in range(3000):
        lsm.put(encode((n,)), b"x" * 20)
    lsm.flush()
    assert lsm.stats["moves"] > 0  # files with no overlap below just change level
    assert all(lsm.get(encode((n,))) == b"x" * 20 for n in range(0, 3000, 7))
