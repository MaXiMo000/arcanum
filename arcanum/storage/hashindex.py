"""A hash index: extendible hashing over pager pages.

Keys are hashed, and the low `global_depth` bits of the hash pick a slot in
the directory, which points at a bucket page. A lookup is one page read,
however large the index grows. A full bucket splits in two on one more hash
bit; when the bucket already uses every directory bit, the directory doubles
first (its slots are pointers, so doubling copies pointers, not data). Buckets
never need rebalancing, but the hash scatters neighbouring keys: there is no
order, so a range of keys means reading every bucket.

The directory is kept in memory, as most implementations keep it cached; only
buckets are pages. Each bucket page holds its local depth and its entries.
"""

import struct
import zlib

from .pager import PAGE_SIZE

_HEADER = struct.Struct("<HH")  # local depth, entry count
_LENGTHS = struct.Struct("<HH")  # key length, value length
MAX_DEPTH = 24  # a directory of 16M slots: beyond this, keys that share a hash cannot be split apart


def _hash(key):
    return zlib.crc32(key)


class HashIndex:
    def __init__(self, pager):
        self.pager = pager
        self.global_depth = 0
        self.directory = [self._new_bucket(0, {})]
        self.splits = 0
        self.doublings = 0

    def get(self, key):
        """The value stored under key, or None: one bucket page read."""
        return self._read(self._slot(key))[1].get(key)

    def put(self, key, value):
        if len(key) + len(value) + _LENGTHS.size + _HEADER.size > PAGE_SIZE:
            raise ValueError("an entry must fit in one bucket page")
        while True:
            slot = self._slot(key)
            depth, entries = self._read(slot)
            entries[key] = value
            if _size(entries) <= PAGE_SIZE:
                self._write(self.directory[slot], depth, entries)
                return
            self._split(slot, depth, entries)

    def scan(self):
        """Every (key, value), in no particular order: each bucket page read once."""
        for page in dict.fromkeys(self.directory):
            yield from _decode(self.pager.read(page))[1].items()

    def pages(self):
        return len(set(self.directory))

    def _slot(self, key):
        return _hash(key) & ((1 << self.global_depth) - 1)

    def _read(self, slot):
        return _decode(self.pager.read(self.directory[slot]))

    def _split(self, slot, depth, entries):
        if depth >= MAX_DEPTH:
            raise ValueError("too many keys share a hash to split this bucket")
        if depth == self.global_depth:
            self.directory += self.directory  # one more bit: both halves point where they did
            self.global_depth += 1
            self.doublings += 1
        old = self.directory[slot]
        bit = 1 << depth
        keep = {k: v for k, v in entries.items() if not _hash(k) & bit}
        move = {k: v for k, v in entries.items() if _hash(k) & bit}
        self._write(old, depth + 1, keep)
        new = self._new_bucket(depth + 1, move)
        for i, page in enumerate(self.directory):
            if page == old and i & bit:
                self.directory[i] = new
        self.splits += 1

    def _new_bucket(self, depth, entries):
        page = self.pager.allocate()
        self._write(page, depth, entries)
        return page

    def _write(self, page, depth, entries):
        self.pager.write(page, _encode(depth, entries))


def _size(entries):
    return _HEADER.size + sum(_LENGTHS.size + len(k) + len(v) for k, v in entries.items())


def _encode(depth, entries):
    out = bytearray(_HEADER.pack(depth, len(entries)))
    for key, value in entries.items():
        out += _LENGTHS.pack(len(key), len(value)) + key + value
    return bytes(out)


def _decode(data):
    depth, count = _HEADER.unpack_from(data)
    entries, i = {}, _HEADER.size
    for _ in range(count):
        klen, vlen = _LENGTHS.unpack_from(data, i)
        i += _LENGTHS.size
        key = data[i:i + klen]
        entries[key] = data[i + klen:i + klen + vlen]
        i += klen + vlen
    return depth, entries
