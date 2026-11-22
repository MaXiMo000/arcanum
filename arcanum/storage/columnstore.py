"""A column store: each column of a table kept in its own pages, compressed.

Rows are appended in order and numbered from 0. Every column is a chain of
pages, and each page holds a run of that column's values: as plain values, or,
when neighbouring values repeat (a category, a status), as (value, run length)
pairs, run-length encoding, whichever is smaller. Next to each page the store
keeps its first row number and the smallest and largest value in it (a zone
map), in memory like a directory.

That layout makes a scan of one column read only that column's pages, often
far fewer than a row store's, and lets a range filter skip every page whose
min/max cannot match. The price is the single row: fetching it reads one page
from every column, and an insert appends to every column.
"""

import bisect
import struct

from .codec import decode, encode
from .pager import PAGE_SIZE

_HEADER = struct.Struct("<BH")  # encoding, length of the encoded values
_PLAIN, _RUNS = 0, 1
_ROOM = PAGE_SIZE - _HEADER.size


class Column:
    def __init__(self, pager):
        self.pager = pager
        self.starts = []  # first row number of each page
        self.pages = []
        self.zones = []  # (min, max) of each page's non-NULL values, or None when all are NULL
        self.open = []  # values of the last page, written by flush()
        self.plain = 0  # bytes the open page takes as plain values
        self.runs = 0  # ... and as (value, run length) pairs
        self.dirty = False
        self.count = 0

    def append(self, value):
        size = len(encode((value,)))
        if size + _RUN_LENGTH > _ROOM:
            raise ValueError("a column value must fit in one page")
        same = bool(self.open) and self.open[-1] == value
        plain, runs = self.plain + size, self.runs + (0 if same else size + _RUN_LENGTH)
        if self.open and min(plain, runs) > _ROOM:
            self.flush()  # the page is full: the value starts the next one
            self.open, plain, runs = [], size, size + _RUN_LENGTH
        if not self.open:
            self.starts.append(self.count)
            self.pages.append(self.pager.allocate())
            self.zones.append(None)
        self.open.append(value)
        self.plain, self.runs, self.dirty = plain, runs, True
        self.count += 1

    def flush(self):
        """Write the page being filled."""
        if not self.dirty:
            return
        self.pager.write(self.pages[-1], _pack(self.open))
        present = [v for v in self.open if v is not None]
        self.zones[-1] = (min(present), max(present)) if present else None
        self.dirty = False

    def get(self, row):
        """The value in row `row`: one page read."""
        self.flush()
        i = bisect.bisect_right(self.starts, row) - 1
        return _unpack(self.pager.read(self.pages[i]))[row - self.starts[i]]

    def slice(self, lo, hi):
        """The values of rows lo..hi (inclusive), reading only the pages that hold them."""
        self.flush()
        first = max(0, bisect.bisect_right(self.starts, lo) - 1)
        last = bisect.bisect_right(self.starts, hi)
        out = []
        for start, page in zip(self.starts[first:last], self.pages[first:last], strict=True):
            values = _unpack(self.pager.read(page))
            out += values[max(0, lo - start):max(0, hi - start + 1)]
        return out

    def scan(self, keep=None):
        """(row, value) for every row; with keep(min, max), pages whose zone cannot match are skipped."""
        self.flush()
        for start, page, zone in zip(self.starts, self.pages, self.zones, strict=True):
            if keep is not None and (zone is None or not keep(*zone)):
                continue
            for offset, value in enumerate(_unpack(self.pager.read(page))):
                yield start + offset, value


class ColumnStore:
    def __init__(self, pager, columns):
        self.names = list(columns)
        self.columns = {name: Column(pager) for name in self.names}
        self.rows = 0

    def append(self, row):
        for name, value in zip(self.names, row, strict=True):
            self.columns[name].append(value)
        self.rows += 1

    def flush(self):
        for column in self.columns.values():
            column.flush()

    def row(self, number):
        """Row `number` (from 0) as a tuple: one page from every column."""
        return tuple(self.columns[name].get(number) for name in self.names)

    def rows_between(self, lo, hi):
        """Rows lo..hi (inclusive) as tuples: each column reads only the pages that hold them."""
        return list(zip(*(self.columns[name].slice(lo, hi) for name in self.names), strict=True))

    def pages(self):
        return sum(len(c.pages) for c in self.columns.values())


_RUN_LENGTH = len(encode((1,)))  # an integer run length; the codec gives every integer the same size


def _pack(values):
    runs = []
    for value in values:
        if runs and runs[-1][0] == value:
            runs[-1][1] += 1
        else:
            runs.append([value, 1])
    plain = encode(values)
    paired = encode([x for run in runs for x in run])
    kind, body = (_RUNS, paired) if len(paired) < len(plain) else (_PLAIN, plain)
    return _HEADER.pack(kind, len(body)) + body


def _unpack(data):
    kind, length = _HEADER.unpack_from(data)
    body = decode(data[_HEADER.size:_HEADER.size + length])
    if kind == _PLAIN:
        return list(body)
    values = []
    for i in range(0, len(body), 2):
        values.extend([body[i]] * body[i + 1])
    return values
