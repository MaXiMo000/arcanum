"""Fixed-size pages behind an LRU buffer pool, made durable by a write-ahead log.

Page 0 holds the file header; every other page belongs to a B+tree or to the
free list. read() hands out immutable bytes and every change goes back through
write(), so the pool is the only place a page is modified.

Durability: commit() appends every changed page and then the header (the
commit frame) to the write-ahead log and syncs it. The data file only ever
receives committed pages, copied over by checkpoint(). A dirty page evicted
from the pool mid-transaction is written to the log without a commit frame,
so it counts only if its transaction commits. Opening a database replays the
committed part of the log (see wal.py).

Savepoints make a group of writes undoable: before a page is first changed
inside a savepoint, its original bytes are kept in memory, and rollback()
puts them back. Statements and transactions are built on this.
"""

import os
import struct
from collections import OrderedDict

from ..errors import ArcanumError, ExecutionError
from ..events import EventBus
from .files import OsFileSystem
from .wal import Wal

PAGE_SIZE = 4096
MAGIC = b"ARCANUM\x00"
VERSION = 1

_HEADER = struct.Struct("<8sHII")  # magic, version, page_count, free_list_head
_NEXT_FREE = struct.Struct("<I")  # a free page starts with the id of the next free page


class CorruptDatabaseError(ArcanumError):
    pass


class Pager:
    def __init__(self, path, pool_size=256, events=None, fs=None, checkpoint_frames=1000, max_pages=None):
        """checkpoint_frames: copy the log into the data file once it holds this many committed frames.
        max_pages: refuse to grow the file beyond this many pages (None = no limit)."""
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        self.path = os.fspath(path)
        self.pool_size = pool_size
        self.events = events if events is not None else EventBus()
        self.fs = fs if fs is not None else OsFileSystem()
        self.checkpoint_frames = checkpoint_frames
        self.max_pages = max_pages
        self.stats = {"hits": 0, "misses": 0, "evictions": 0, "disk_reads": 0, "wal_frames": 0, "checkpoints": 0}
        self.recovered_frames = 0  # committed frames replayed from the log when the file was opened
        self._pool = OrderedDict()  # page id -> [data, dirty], least recently used first
        self._savepoints = []  # [(original page images, page_count, free_head)], innermost last
        self._closed = False
        self._file = self.fs.open(self.path)
        if self._file.size() < PAGE_SIZE:
            # New database (or one whose creation never finished: nothing was ever committed to it).
            self.page_count, self.free_head = 1, 0
            self._file.truncate(0)
            self._file.write(0, self._header_page())
            self._file.sync()
            self.wal = Wal(self.fs.open(self.path + "-wal"), PAGE_SIZE, self.events)
            self.wal.reset()
        else:
            self._read_header()  # rejects files that are not Arcanum databases before touching anything
            self.wal = Wal(self.fs.open(self.path + "-wal"), PAGE_SIZE, self.events)
            self.recovered_frames = self.wal.recover()
            if self.recovered_frames:
                self.events.emit("recovery", frames=self.recovered_frames, pages=len(self.wal.committed_pages))
                self.checkpoint()
                self._read_header()
        self._committed_header = (self.page_count, self.free_head)

    # ---- pages -----------------------------------------------------------------

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
        data = self._stored(page_id)
        self.events.emit("page_read", page=page_id, source="disk")
        self._put(page_id, data, dirty=False)
        return data

    def write(self, page_id, data):
        self._check(page_id)
        if len(data) > PAGE_SIZE:
            raise ValueError(f"page {page_id}: {len(data)} bytes does not fit in a {PAGE_SIZE}-byte page")
        if self._savepoints:
            originals, page_count, _ = self._savepoints[-1]
            if page_id < page_count and page_id not in originals:
                originals[page_id] = self._current(page_id)
        self._put(page_id, bytes(data).ljust(PAGE_SIZE, b"\x00"), dirty=True)
        self.events.emit("page_write", page=page_id)

    def allocate(self) -> int:
        """Return a zeroed page, reusing a freed one when possible."""
        if self.free_head:
            page_id = self.free_head
            self.free_head = _NEXT_FREE.unpack_from(self.read(page_id))[0]
        else:
            if self.max_pages is not None and self.page_count >= self.max_pages:
                raise ExecutionError(f"the database is full (limit {self.max_pages} pages)")
            page_id = self.page_count
            self.page_count += 1
        self.write(page_id, b"")
        self.events.emit("page_alloc", page=page_id)
        return page_id

    def free(self, page_id):
        self.write(page_id, _NEXT_FREE.pack(self.free_head))
        self.free_head = page_id
        self.events.emit("page_free", page=page_id)

    # ---- savepoints --------------------------------------------------------------

    def savepoint(self):
        """Start a group of writes that rollback() can undo. Savepoints nest.

        An outermost savepoint first commits any changes made outside savepoints,
        so rolling it back never discards work it did not contain.
        """
        if not self._savepoints:
            self.commit()
        self._savepoints.append(({}, self.page_count, self.free_head))

    def release(self):
        """Keep the writes of the innermost savepoint (they join the enclosing one, if any)."""
        originals, _, _ = self._savepoints.pop()
        if self._savepoints:
            outer = self._savepoints[-1][0]
            for page_id, data in originals.items():
                outer.setdefault(page_id, data)

    def rollback(self):
        """Undo every write since the innermost savepoint and end it."""
        originals, page_count, free_head = self._savepoints.pop()
        # Drop every page the savepoint touched before restoring any, so restoring one can
        # never evict another that still holds rolled-back changes. Pages allocated inside
        # the savepoint no longer exist at all.
        for page_id in [p for p in self._pool if p >= page_count or p in originals]:
            del self._pool[page_id]
        self.page_count, self.free_head = page_count, free_head
        outermost = not self._savepoints
        if outermost:
            self.wal.rollback()  # pages spilled to the log by this transaction no longer count
        for page_id, data in originals.items():
            # An outermost savepoint began at a commit, so its originals are exactly what is
            # stored: they come back clean. Inside a transaction they still need writing.
            self._put(page_id, data, dirty=not outermost)
        self.events.emit("rollback", pages=len(originals))

    @property
    def in_savepoint(self):
        return bool(self._savepoints)

    # ---- durability --------------------------------------------------------------

    def commit(self):
        """Make every change so far durable: log the dirty pages and a commit frame, then sync the log."""
        if self._savepoints:
            raise RuntimeError("cannot commit while a savepoint is open")
        dirty = [(page_id, entry[0]) for page_id, entry in sorted(self._pool.items()) if entry[1]]
        header = (self.page_count, self.free_head)
        if not dirty and header == self._committed_header and not self.wal.has_uncommitted:
            return
        for page_id, data in dirty:
            self.wal.append(page_id, data)
        self.wal.append(0, self._header_page(), commit=True)
        self.wal.commit()
        for page_id, _ in dirty:
            self._pool[page_id][1] = False
        self._committed_header = header
        self.stats["wal_frames"] += len(dirty) + 1
        self.events.emit("commit", pages=len(dirty))
        if self.wal.committed_frames >= self.checkpoint_frames:
            self.checkpoint()

    def checkpoint(self):
        """Copy committed pages from the log into the data file, sync it, then empty the log."""
        if self._savepoints or self.wal.has_uncommitted:
            raise RuntimeError("cannot checkpoint while a transaction is open")
        pages = self.wal.committed_pages
        for page_id, offset in sorted(pages.items()):
            self._file.write(page_id * PAGE_SIZE, self.wal.read(offset))
        self._file.sync()
        self.wal.reset()
        self.stats["checkpoints"] += 1
        self.events.emit("checkpoint", pages=len(pages))

    def close(self):
        """Commit outstanding changes, checkpoint and remove the log. Open savepoints are rolled back."""
        if self._closed:
            return
        while self._savepoints:
            self.rollback()
        self.commit()
        self.checkpoint()
        self.wal.close()
        self._file.close()
        self.fs.remove(self.path + "-wal")
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- internals ----------------------------------------------------------------

    def _put(self, page_id, data, dirty):
        entry = self._pool.get(page_id)
        self._pool[page_id] = [data, dirty or (entry is not None and entry[1])]
        self._pool.move_to_end(page_id)
        while len(self._pool) > self.pool_size:
            victim, (victim_data, victim_dirty) = self._pool.popitem(last=False)
            if victim_dirty:
                self.wal.append(victim, victim_data)  # uncommitted until the next commit frame
                self.stats["wal_frames"] += 1
            self.stats["evictions"] += 1
            self.events.emit("cache_evict", page=victim, dirty=victim_dirty)

    def _stored(self, page_id):
        """The newest stored image of a page: from the log if it is there, else the data file."""
        offset = self.wal.index.get(page_id)
        if offset is not None:
            return self.wal.read(offset)
        data = self._file.read(page_id * PAGE_SIZE, PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise CorruptDatabaseError(f"page {page_id} is truncated")
        return data

    def peek(self, page_id):
        """A page's bytes without touching LRU order, stats or events (for inspection)."""
        self._check(page_id)
        return self._current(page_id)

    def cached_pages(self):
        """[(page id, dirty)] in the pool, least recently used first."""
        return [(page_id, entry[1]) for page_id, entry in self._pool.items()]

    def free_pages(self):
        """Page ids on the free list, in list order."""
        pages, page_id = [], self.free_head
        while page_id and len(pages) < self.page_count:
            pages.append(page_id)
            page_id = _NEXT_FREE.unpack_from(self._current(page_id))[0]
        return pages

    def _current(self, page_id):
        """A page's bytes without touching LRU order, stats or events."""
        entry = self._pool.get(page_id)
        return entry[0] if entry is not None else self._stored(page_id)

    def _check(self, page_id):
        if not 1 <= page_id < self.page_count:
            raise IndexError(f"page {page_id} does not exist (file has {self.page_count} pages)")

    def _header_page(self):
        return _HEADER.pack(MAGIC, VERSION, self.page_count, self.free_head).ljust(PAGE_SIZE, b"\x00")

    def _read_header(self):
        raw = self._file.read(0, _HEADER.size)
        magic, version, self.page_count, self.free_head = _HEADER.unpack(raw)
        if magic != MAGIC:
            self._file.close()
            raise CorruptDatabaseError(f"{self.path} is not an Arcanum database")
        if version != VERSION:
            self._file.close()
            raise CorruptDatabaseError(f"{self.path} uses format version {version}, expected {VERSION}")
