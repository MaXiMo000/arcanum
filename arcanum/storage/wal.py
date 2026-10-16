"""Write-ahead log of page images.

A transaction commits by appending the new image of every page it changed,
then a frame holding the file header marked as the commit, and syncing the
log. The data file is not touched until a checkpoint copies the logged pages
into it, so a crash at any moment leaves either the old or the new state.

Layout:
    header  magic (8s), page size (u32), salt (u32), checksum of the first 16 bytes (u32)
    frame   page id (u32), commit flag (u32), salt (u32), checksum (u32), page image

Each frame's checksum is a CRC-32 chained from the previous frame's, so a
torn or missing write breaks the chain and nothing after it is trusted. The
salt changes whenever the log is reset, so frames left over from an earlier
generation of the log are never mistaken for current ones.
"""

import os
import struct
import zlib

MAGIC = b"ARCWAL\x00\x01"
_HEADER = struct.Struct("<8sIII")
_FRAME_HEAD = struct.Struct("<IIII")
_U32 = struct.Struct("<I")


class Wal:
    def __init__(self, file, page_size, events):
        self.file = file
        self.page_size = page_size
        self.frame_size = _FRAME_HEAD.size + page_size
        self.events = events
        self.index = {}  # page id -> offset of its newest image, including the open transaction's
        self._committed = {}  # the same, as of the last commit
        self.salt = 0
        self._tail = self._end = 0  # end of all frames / end of committed frames
        self._crc = self._commit_crc = 0

    @property
    def committed_pages(self):
        return self._committed

    @property
    def committed_frames(self):
        return (self._end - _HEADER.size) // self.frame_size

    @property
    def has_uncommitted(self):
        return self._tail != self._end

    def read(self, offset):
        return self.file.read(offset, self.page_size)

    def append(self, page_id, data, commit=False):
        head = struct.pack("<III", page_id, int(commit), self.salt)
        crc = zlib.crc32(head + data, self._crc)
        offset = self._tail
        self.file.write(offset, head + _U32.pack(crc) + data)
        self.index[page_id] = offset + _FRAME_HEAD.size
        self._tail += self.frame_size
        self._crc = crc
        self.events.emit("wal_append", page=page_id, commit=commit, offset=offset)

    def commit(self):
        """Make every appended frame durable. Call after appending the commit frame."""
        self.file.sync()
        self._committed = dict(self.index)
        self._end, self._commit_crc = self._tail, self._crc
        self.events.emit("wal_sync", frames=self.committed_frames)

    def rollback(self):
        """Forget frames appended since the last commit."""
        self.file.truncate(self._end)
        self.index = dict(self._committed)
        self._tail, self._crc = self._end, self._commit_crc

    def reset(self):
        """Start an empty log with a new salt (after a checkpoint)."""
        self.salt = int.from_bytes(os.urandom(4), "little")
        first = _HEADER.pack(MAGIC, self.page_size, self.salt, 0)[:16]
        header_crc = zlib.crc32(first)
        self.file.truncate(0)
        self.file.write(0, first + _U32.pack(header_crc))
        self.file.sync()
        self.index, self._committed = {}, {}
        self._tail = self._end = _HEADER.size
        self._crc = self._commit_crc = header_crc

    def recover(self):
        """Load the committed frames of an existing log; drop anything after the last commit.

        Returns the number of committed frames found.
        """
        size = self.file.size()
        raw = self.file.read(0, _HEADER.size) if size >= _HEADER.size else b""
        if len(raw) < _HEADER.size:
            self.reset()
            return 0
        magic, page_size, salt, header_crc = _HEADER.unpack(raw)
        if magic != MAGIC or page_size != self.page_size or header_crc != zlib.crc32(raw[:16]):
            self.reset()
            return 0
        self.salt = salt
        crc, offset = header_crc, _HEADER.size
        pending, committed = {}, {}
        end, end_crc = offset, crc
        while offset + self.frame_size <= size:
            frame = self.file.read(offset, self.frame_size)
            page_id, commit, frame_salt, frame_crc = _FRAME_HEAD.unpack_from(frame)
            if frame_salt != salt or zlib.crc32(frame[:12] + frame[16:], crc) != frame_crc:
                break  # torn, missing or stale frame: the log ends here
            crc = frame_crc
            pending[page_id] = offset + _FRAME_HEAD.size
            offset += self.frame_size
            if commit:
                committed.update(pending)
                pending = {}
                end, end_crc = offset, crc
        self.file.truncate(end)
        self._committed, self.index = committed, dict(committed)
        self._tail, self._end = end, end
        self._crc = self._commit_crc = end_crc
        return self.committed_frames

    def close(self):
        self.file.close()
