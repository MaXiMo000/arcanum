"""File access behind a small interface.

The pager and write-ahead log only use these operations, so tests can swap in
a file system that simulates power loss (see tests/crashfs.py).
"""

import contextlib
import os
from collections import Counter


class OsFile:
    def __init__(self, path):
        # Unbuffered, so size() and reads always see every write.
        self._f = open(path, "r+b" if os.path.exists(path) else "w+b", buffering=0)

    def size(self):
        return os.fstat(self._f.fileno()).st_size

    def read(self, offset, size):
        self._f.seek(offset)
        return self._f.read(size)

    def write(self, offset, data):
        self._f.seek(offset)
        view = memoryview(data)
        while view:
            view = view[self._f.write(view):]

    def truncate(self, size):
        self._f.truncate(size)

    def sync(self):
        os.fsync(self._f.fileno())

    def close(self):
        self._f.close()


class OsFileSystem:
    def open(self, path):
        return OsFile(path)

    def remove(self, path):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


class MemFile:
    def __init__(self, fs, path):
        self.fs, self.path = fs, path
        self.data = fs.files.setdefault(path, bytearray())

    def size(self):
        return len(self.data)

    def read(self, offset, size):
        self.fs.count(self.path, "read", size)
        return bytes(self.data[offset:offset + size])

    def write(self, offset, data):
        self.fs.count(self.path, "write", len(data))
        if offset > len(self.data):
            self.data.extend(bytes(offset - len(self.data)))
        self.data[offset:offset + len(data)] = data

    def truncate(self, size):
        del self.data[size:]

    def sync(self):
        self.fs.count(self.path, "sync", 0)

    def close(self):
        pass


class CountingMemFileSystem:
    """Files in memory that count every read, write and sync, to compare how much I/O storage engines do
    on the same workload. Counts are kept per kind of file: "log" (write-ahead logs) and "data"."""

    def __init__(self):
        self.files = {}  # path -> bytearray
        self.io = Counter()

    def open(self, path):
        return MemFile(self, path)

    def remove(self, path):
        self.files.pop(path, None)

    def count(self, path, op, size):
        kind = "log" if path.endswith("wal") else "data"
        self.io[f"{op}s"] += 1
        self.io[f"{op}_bytes"] += size
        self.io[f"{kind}_{op}_bytes"] += size

    def size(self):
        return sum(len(data) for data in self.files.values())
