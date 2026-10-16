import pytest

from arcanum.storage.pager import PAGE_SIZE, CorruptDatabaseError, Pager


def test_write_read_and_reopen(tmp_path):
    path = tmp_path / "t.db"
    with Pager(path) as pager:
        a, b = pager.allocate(), pager.allocate()
        pager.write(a, b"alpha")
        pager.write(b, b"beta")
    with Pager(path) as pager:
        assert pager.page_count == 3
        assert pager.read(a).rstrip(b"\x00") == b"alpha"
        assert pager.read(b).rstrip(b"\x00") == b"beta"
        assert len(pager.read(a)) == PAGE_SIZE


def test_lru_eviction_writes_dirty_pages(tmp_path):
    events = []
    with Pager(tmp_path / "t.db", pool_size=2) as pager:
        pager.events.subscribe(lambda kind, data: events.append((kind, data)))
        pages = [pager.allocate() for _ in range(4)]
        for p in pages:
            pager.write(p, bytes([p]))
        assert pager.stats["evictions"] >= 2
        assert ("cache_evict", {"page": pages[0], "dirty": True}) in events
        # Evicted pages come back from disk with their contents intact.
        assert pager.read(pages[0])[0] == pages[0]
        assert pager.stats["misses"] >= 1


def test_cache_hits_are_counted(tmp_path):
    with Pager(tmp_path / "t.db") as pager:
        p = pager.allocate()
        pager.read(p)
        pager.read(p)
        assert pager.stats["hits"] == 2
        assert pager.stats["misses"] == 0


def test_freed_pages_are_reused(tmp_path):
    path = tmp_path / "t.db"
    with Pager(path) as pager:
        a, b = pager.allocate(), pager.allocate()
        pager.free(a)
        pager.free(b)
    with Pager(path) as pager:  # the free list survives a reopen
        assert {pager.allocate(), pager.allocate()} == {a, b}
        assert pager.read(a) == bytes(PAGE_SIZE)
        assert pager.allocate() == 3


def test_rejects_bad_access(tmp_path):
    with Pager(tmp_path / "t.db") as pager:
        with pytest.raises(IndexError):
            pager.read(0)  # the header is not a data page
        with pytest.raises(IndexError):
            pager.read(5)
        p = pager.allocate()
        with pytest.raises(ValueError):
            pager.write(p, bytes(PAGE_SIZE + 1))


def test_rejects_foreign_files(tmp_path):
    path = tmp_path / "other.db"
    path.write_bytes(b"SQLite format 3\x00" + bytes(PAGE_SIZE))
    with pytest.raises(CorruptDatabaseError):
        Pager(path)
