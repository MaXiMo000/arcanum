"""File access behind a small interface.

The pager and write-ahead log only use these operations, so tests can swap in
a file system that simulates power loss (see tests/crashfs.py).
"""

import contextlib
import os


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
