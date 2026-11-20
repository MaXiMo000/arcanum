"""A log-structured merge tree (LSM tree), the design behind RocksDB, LevelDB and Cassandra.

Where the B+tree changes pages in place, an LSM tree never overwrites
anything. Writes go to a log and to an in-memory table (the memtable); a full
memtable is written out, sorted, as an immutable file (an SSTable) in level 0.
Level 0 files may overlap; every deeper level is one sorted run of
non-overlapping files, each level `ratio` times larger than the one above.
Compaction merges files into the level below, keeping the newest value of each
key and dropping deletions (tombstones) once nothing older can hide under them.

Writes are cheap and sequential, paid for later by compaction rewriting the
same data level after level (write amplification). A lookup may have to check
several files (read amplification); each file's Bloom filter answers "surely
not here" for most of them without reading a block.

SSTable layout: data blocks of entries (u16 key length, key, u32 value length
or TOMBSTONE, value), then the block index (each block's first key and
position) and the Bloom filter. The index and filter are also kept in memory,
as real engines do.
"""

import hashlib
import struct
from bisect import bisect_right
from collections import OrderedDict

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
TOMBSTONE = 0xFFFFFFFF
BLOOM_BITS_PER_KEY = 10
BLOOM_HASHES = 7  # about 1% false positives at 10 bits per key


def _entry(key, value):
    if value is None:
        return _U16.pack(len(key)) + key + _U32.pack(TOMBSTONE)
    return _U16.pack(len(key)) + key + _U32.pack(len(value)) + value


def _entries(block):
    """[(key, value or None)] of an encoded block."""
    out, pos = [], 0
    while pos < len(block):
        (key_len,) = _U16.unpack_from(block, pos)
        key = bytes(block[pos + 2:pos + 2 + key_len])
        pos += 2 + key_len
        (value_len,) = _U32.unpack_from(block, pos)
        pos += 4
        if value_len == TOMBSTONE:
            out.append((key, None))
        else:
            out.append((key, bytes(block[pos:pos + value_len])))
            pos += value_len
    return out


class Bloom:
    def __init__(self, keys):
        self.size = max(64, len(keys) * BLOOM_BITS_PER_KEY)
        self.bits = bytearray((self.size + 7) // 8)
        for key in keys:
            for bit in self._bits(key):
                self.bits[bit >> 3] |= 1 << (bit & 7)

    def _bits(self, key):
        digest = hashlib.blake2b(key, digest_size=16).digest()
        h1, h2 = int.from_bytes(digest[:8], "little"), int.from_bytes(digest[8:], "little") | 1
        return [(h1 + i * h2) % self.size for i in range(BLOOM_HASHES)]

    def may_contain(self, key):
        return all(self.bits[bit >> 3] & (1 << (bit & 7)) for bit in self._bits(key))


class SSTable:
    def __init__(self, number, path, index, bloom, entries, size, first, last):
        self.number, self.path, self.index, self.bloom = number, path, index, bloom
        self.entries, self.size, self.first, self.last = entries, size, first, last  # first/last: key range

    def overlaps(self, lo, hi):
        return not (self.last < lo or self.first > hi)


class LSMTree:
    def __init__(self, fs, directory, memtable_bytes=64 * 1024, block_size=4096, level0_files=4, ratio=10,
                 cache_blocks=16, on_change=None):
        """memtable_bytes: flush the memtable at this size (also the target SSTable size).
        level0_files: compact level 0 once it holds this many files. ratio: each deeper level may hold this
        many times more bytes. cache_blocks: blocks kept in memory (LRU), like the B+tree's buffer pool.
        on_change(kind, detail): called after every flush and compaction."""
        self.fs, self.directory = fs, directory
        self.memtable_bytes, self.block_size = memtable_bytes, block_size
        self.level0_files, self.ratio, self.cache_blocks = level0_files, ratio, cache_blocks
        self.on_change = on_change or (lambda kind, detail: None)
        self.memtable, self.memtable_size = {}, 0
        self.levels = [[]]  # level 0: newest last; deeper: sorted by key, non-overlapping
        self.cache = OrderedDict()  # (table number, block number) -> entries
        self.numbers = 0
        self.cursors = {}  # level -> last key pushed down from it
        self.stats = {"flushes": 0, "compactions": 0, "moves": 0, "bloom_skips": 0, "block_reads": 0, "tombstones_dropped": 0}
        self._log = fs.open(f"{directory}/lsm-wal")
        self._log_size, self._pending = 0, bytearray()

    # ---- writes ------------------------------------------------------------------------------

    def put(self, key, value):
        self._pending += _entry(key, value)
        if key in self.memtable:
            self.memtable_size -= len(key) + len(self.memtable[key] or b"")
        self.memtable[key] = value
        self.memtable_size += len(key) + len(value or b"")
        if self.memtable_size >= self.memtable_bytes:
            self.flush()

    def delete(self, key):
        self.put(key, None)

    def commit(self):
        """Make the writes so far durable: append them to the log and sync it."""
        if self._pending:
            self._log.write(self._log_size, self._pending)
            self._log_size += len(self._pending)
            self._pending.clear()
            self._log.sync()

    def flush(self):
        """Write the memtable out as a level-0 SSTable; its log is no longer needed."""
        if not self.memtable:
            return
        self.commit()
        table = self._write(sorted(self.memtable.items()))
        self.levels[0].append(table)
        self.memtable, self.memtable_size = {}, 0
        self._log.truncate(0)
        self._log_size = 0
        self.stats["flushes"] += 1
        self.on_change("flush", {"table": table.number, "bytes": table.size, "entries": table.entries})
        self._compact()

    # ---- reads -------------------------------------------------------------------------------

    def get(self, key):
        """The value under key, or None. Newest first: the memtable, level 0 newest to oldest, then each
        deeper level, where at most one file can hold the key."""
        if key in self.memtable:
            return self.memtable[key]
        for level, tables in enumerate(self.levels):
            candidates = reversed(tables) if level == 0 else self._covering(tables, key)
            for table in candidates:
                if not table.first <= key <= table.last:
                    continue
                if not table.bloom.may_contain(key):
                    self.stats["bloom_skips"] += 1
                    continue
                block = bisect_right([k for k, _, _ in table.index], key) - 1
                for k, v in self._block(table, block):
                    if k == key:
                        return v  # a tombstone (None) hides older values: the key is deleted
        return None

    def scan(self, lo, hi):
        """[(key, value)] for lo <= key <= hi, merged from every source, newest winning."""
        merged = {}
        for level in range(len(self.levels) - 1, -1, -1):  # oldest first, so newer values overwrite
            for table in self.levels[level]:
                if not table.overlaps(lo, hi):
                    continue
                starts = [k for k, _, _ in table.index]
                first, last = max(0, bisect_right(starts, lo) - 1), bisect_right(starts, hi)
                for block in range(first, last):
                    merged.update((k, v) for k, v in self._block(table, block) if lo <= k <= hi)
        merged.update((k, v) for k, v in self.memtable.items() if lo <= k <= hi)
        return [(k, v) for k, v in sorted(merged.items()) if v is not None]

    def shape(self):
        """The memtable and every level's files, for drawing."""
        return {"memtable": {"entries": len(self.memtable), "bytes": self.memtable_size, "limit": self.memtable_bytes},
                "levels": [[{"number": t.number, "bytes": t.size, "entries": t.entries, "first": t.first,
                             "last": t.last} for t in tables] for tables in self.levels],
                "capacity": [None, *(self._capacity(level) for level in range(1, len(self.levels)))]}

    def memory(self):
        """Bytes of Bloom filters and block indexes held in memory."""
        return sum(len(t.bloom.bits) + sum(len(k) + 8 for k, _, _ in t.index) for ts in self.levels for t in ts)

    # ---- compaction --------------------------------------------------------------------------

    def _capacity(self, level):
        return self.memtable_bytes * self.level0_files * self.ratio ** (level - 1)

    def _compact(self):
        while True:
            if len(self.levels[0]) >= self.level0_files:
                self._merge(0, list(self.levels[0]))
                continue
            full = next((lv for lv in range(1, len(self.levels))
                         if sum(t.size for t in self.levels[lv]) > self._capacity(lv)), None)
            if full is None:
                return
            # Push one file down, rotating through the key space as LevelDB does.
            tables = self.levels[full]
            cursor = self.cursors.get(full, b"")
            table = next((t for t in tables if t.first > cursor), tables[0])
            self.cursors[full] = table.last
            self._merge(full, [table])

    def _merge(self, level, inputs):
        """Merge inputs (files of `level`) with the overlapping files of the level below into new files there."""
        target = level + 1
        if target == len(self.levels):
            self.levels.append([])
        lo, hi = min(t.first for t in inputs), max(t.last for t in inputs)
        below = [t for t in self.levels[target] if t.overlaps(lo, hi)]
        if level > 0 and len(inputs) == 1 and not below:
            # Nothing to merge with: move the file down as it is, without rewriting it (LevelDB's trivial move).
            table = inputs[0]
            self.levels[level] = [t for t in self.levels[level] if t is not table]
            self.levels[target] = sorted([*self.levels[target], table], key=lambda t: t.first)
            self.stats["moves"] += 1
            self.on_change("move", {"from": level, "to": target, "table": table.number, "bytes": table.size})
            return
        merged = {}
        for table in [*below, *inputs]:  # oldest first: below, then the inputs (level 0 oldest to newest)
            for block in range(len(table.index)):
                merged.update(self._block(table, block, cache=False))
        bottom = all(not tables for tables in self.levels[target + 1:])
        items = sorted(merged.items())
        if bottom:
            before = len(items)
            items = [(k, v) for k, v in items if v is not None]  # nothing older left for a tombstone to hide
            self.stats["tombstones_dropped"] += before - len(items)
        outputs, chunk, size = [], [], 0
        for key, value in items:
            chunk.append((key, value))
            size += len(key) + len(value or b"") + 6
            if size >= self.memtable_bytes:
                outputs.append(self._write(chunk))
                chunk, size = [], 0
        if chunk:
            outputs.append(self._write(chunk))
        for table in [*inputs, *below]:
            self.fs.remove(table.path)
        gone = {id(t) for t in [*inputs, *below]}
        self.levels[level] = [t for t in self.levels[level] if id(t) not in gone]
        self.levels[target] = sorted([*(t for t in self.levels[target] if id(t) not in gone), *outputs],
                                     key=lambda t: t.first)
        self.stats["compactions"] += 1
        self.on_change("compaction", {"from": level, "to": target, "inputs": [t.number for t in inputs],
                                      "overlapping": [t.number for t in below], "outputs": [t.number for t in outputs],
                                      "bytes_in": sum(t.size for t in [*inputs, *below]),
                                      "bytes_out": sum(t.size for t in outputs)})

    # ---- files -------------------------------------------------------------------------------

    def _write(self, items):
        """Write sorted (key, value) items as a new SSTable."""
        self.numbers += 1
        path = f"{self.directory}/{self.numbers:06d}.sst"
        data, index, block = bytearray(), [], bytearray()
        first_key = None
        for key, value in items:
            entry = _entry(key, value)
            if block and len(block) + len(entry) > self.block_size:
                index.append((first_key, len(data), len(block)))
                data += block
                block = bytearray()
            if not block:
                first_key = key
            block += entry
        if block:
            index.append((first_key, len(data), len(block)))
            data += block
        bloom = Bloom([k for k, _ in items])
        footer = bytearray()
        for key, offset, length in index:
            footer += _U16.pack(len(key)) + key + _U32.pack(offset) + _U32.pack(length)
        footer += bloom.bits + _U32.pack(len(data)) + _U32.pack(len(index))
        file = self.fs.open(path)
        file.write(0, bytes(data + footer))
        file.sync()
        return SSTable(self.numbers, path, index, bloom, len(items), len(data) + len(footer), items[0][0], items[-1][0])

    def _block(self, table, block, cache=True):
        """The entries of one block, through the block cache (compaction reads past it, as RocksDB can)."""
        cache_key = (table.number, block)
        hit = self.cache.get(cache_key)
        if hit is not None:
            self.cache.move_to_end(cache_key)
            return hit
        _, offset, length = table.index[block]
        entries = _entries(self.fs.open(table.path).read(offset, length))
        self.stats["block_reads"] += 1
        if cache and self.cache_blocks:
            self.cache[cache_key] = entries
            if len(self.cache) > self.cache_blocks:
                self.cache.popitem(last=False)
        return entries

    @staticmethod
    def _covering(tables, key):
        """The one file of a sorted, non-overlapping level whose range may hold key."""
        i = bisect_right([t.first for t in tables], key) - 1
        return [tables[i]] if i >= 0 else []
