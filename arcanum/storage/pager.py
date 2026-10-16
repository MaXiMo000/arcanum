"""Fixed-size pages on disk behind an LRU buffer pool.

Page 0 holds the file header; every other page belongs to a B+tree or to the
free list. read() hands out immutable bytes and every change goes back through
write(), so the pool is the only place a page is modified and evicting a dirty
page can never lose an update a caller is still holding.
"""

import os
import struct
from collections import OrderedDict

from ..events import EventBus

PAGE_SIZE = 4096
MAGIC = b"ARCANUM\x00"
VERSION = 1

_HEADER = struct.Struct("<8sHII")  # magic, version, page_count, free_list_head
_NEXT_FREE = struct.Struct("<I")  # a free page starts with the id of the next free page


class CorruptDatabaseError(Exception):
    pass


class Pager:
    def __init__(self, path, pool_size=256, events=None):
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        self.path = path
        self.pool_size = pool_size
        self.events = events if events is not None else EventBus()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0, "disk_reads": 0, "disk_writes": 0}
        self._pool = OrderedDict()  # page id -> [data, dirty], least recently used first
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._file = open(path, "w+b" if fresh else "r+b")
        if fresh:
            self.page_count, self.free_head = 1, 0
            self._write_header()
        else:
            self._read_header()

    def read(self, page_id) -> bytes:
        self._check(page_id)
        entry = self._pool.get(page_id)
        if entry is not None:
            self._pool.move_to_end(page_id)
            self.stats["hits"] += 1
            self.events.emit("page_read", page=page_id, source="cache")
            return entry[0]
        self.stats["misses"] += 1
        self.stats["disk_reads"] += 1
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise CorruptDatabaseError(f"page {page_id} is truncated")
        self.events.emit("page_read", page=page_id, source="disk")
        self._put(page_id, data, dirty=False)
        return data

    def write(self, page_id, data):
        self._check(page_id)
        if len(data) > PAGE_SIZE:
            raise ValueError(f"page {page_id}: {len(data)} bytes does not fit in a {PAGE_SIZE}-byte page")
        self._put(page_id, bytes(data).ljust(PAGE_SIZE, b"\x00"), dirty=True)
        self.events.emit("page_write", page=page_id)

    def allocate(self) -> int:
        """Return a zeroed page, reusing a freed one when possible."""
        if self.free_head:
            page_id = self.free_head
            self.free_head = _NEXT_FREE.unpack_from(self.read(page_id))[0]
        else:
            page_id = self.page_count
            self.page_count += 1
        self.write(page_id, b"")
        self.events.emit("page_alloc", page=page_id)
        return page_id

    def free(self, page_id):
        self.write(page_id, _NEXT_FREE.pack(self.free_head))
        self.free_head = page_id
        self.events.emit("page_free", page=page_id)

    def flush(self):
        """Write every dirty page and the header, then fsync."""
        for page_id, entry in sorted(self._pool.items()):
            if entry[1]:
                self._write_page(page_id, entry[0])
                entry[1] = False
        self._write_header()
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self):
        if not self._file.closed:
            self.flush()
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _put(self, page_id, data, dirty):
        entry = self._pool.get(page_id)
        self._pool[page_id] = [data, dirty or (entry is not None and entry[1])]
        self._pool.move_to_end(page_id)
        while len(self._pool) > self.pool_size:
            victim, (victim_data, victim_dirty) = self._pool.popitem(last=False)
            if victim_dirty:
                self._write_page(victim, victim_data)
            self.stats["evictions"] += 1
            self.events.emit("cache_evict", page=victim, dirty=victim_dirty)

    def _write_page(self, page_id, data):
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)
        self.stats["disk_writes"] += 1

    def _check(self, page_id):
        if not 1 <= page_id < self.page_count:
            raise IndexError(f"page {page_id} does not exist (file has {self.page_count} pages)")

    def _write_header(self):
        self._file.seek(0)
        self._file.write(_HEADER.pack(MAGIC, VERSION, self.page_count, self.free_head).ljust(PAGE_SIZE, b"\x00"))

    def _read_header(self):
        self._file.seek(0)
        raw = self._file.read(PAGE_SIZE)
        if len(raw) < _HEADER.size:
            raise CorruptDatabaseError(f"{self.path} is too short to be an Arcanum database")
        magic, version, self.page_count, self.free_head = _HEADER.unpack_from(raw)
        if magic != MAGIC:
            raise CorruptDatabaseError(f"{self.path} is not an Arcanum database")
        if version != VERSION:
            raise CorruptDatabaseError(f"{self.path} uses format version {version}, expected {VERSION}")
