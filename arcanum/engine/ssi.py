"""Serializable snapshot isolation (SSI), the way PostgreSQL implements SERIALIZABLE.

A SERIALIZABLE transaction runs on one snapshot, like REPEATABLE READ, and on
top of that the engine remembers what it read (PostgreSQL's SIREAD locks):
ranges of a table's primary key for key lookups and key ranges, the whole table
for anything else, as PostgreSQL does for sequential scans.

A read/write dependency (rw-conflict) R -> W means R read something that a
concurrent W wrote, so in any serial order R must come before W. It is found
from either side: W writes a row inside something R read, or R meets a version
W wrote that its snapshot cannot see.

One rw-conflict is harmless. Every anomaly snapshot isolation allows needs a
"pivot": a transaction with a conflict in and a conflict out (T_in -> pivot ->
T_out). As soon as a new conflict creates a pivot, the transaction that made it
is rolled back with "could not serialize access due to read/write dependencies
among transactions", the error PostgreSQL gives. This is Cahill's original
rule; PostgreSQL also requires T_out to commit first, so it aborts a little
less often. Neither ever lets a non-serializable result commit.

A committed transaction's reads are kept while a running SERIALIZABLE
transaction is concurrent with it, since a later write can still conflict.
"""

from dataclasses import dataclass, field

from ..errors import SerializationError

SERIALIZABLE = "SERIALIZABLE"
RANGES_PER_TABLE = 32  # beyond this, a transaction's reads of a table are promoted to the whole table


@dataclass(eq=False)
class Sx:
    """A SERIALIZABLE transaction as SSI sees it."""
    xid: int
    owner: str
    txn: object
    reads: dict = field(default_factory=dict)  # table -> None (the whole table) or [(lo, hi, hi_inclusive)]
    inn: dict = field(default_factory=dict)  # xid -> what: transactions that read what this one wrote
    out: dict = field(default_factory=dict)  # xid -> what: transactions that wrote what this one read
    committed: bool = False


class SSI:
    def __init__(self, mvcc, describe, emit):
        self.mvcc = mvcc
        self.describe = describe  # (table name, key) -> "row (id = 1)"
        self.emit = emit
        self.sx = {}  # xid -> Sx, running or committed but still relevant

    def begin(self, txn):
        if txn.isolation == SERIALIZABLE:
            self.sx[txn.xid] = Sx(txn.xid, txn.owner, txn)

    def end(self, txn, committed):
        """Forget an aborted transaction's conflicts; keep a committed one while it can still matter."""
        sx = self.sx.get(txn.xid)
        if sx is not None:
            if committed:
                sx.committed = True
            else:
                del self.sx[txn.xid]
                for other in self.sx.values():
                    other.inn.pop(txn.xid, None)
                    other.out.pop(txn.xid, None)
        running = [s for s in self.sx.values() if not s.committed]
        for xid in [x for x, s in self.sx.items() if s.committed]:
            if not any(self._concurrent(r, self.sx[xid]) for r in running):
                del self.sx[xid]

    # ---- reads ---------------------------------------------------------------------------------

    def reader(self, snapshot):
        """The Sx reading through snapshot, or None when the reader is not SERIALIZABLE."""
        return self.sx.get(snapshot.own) if self.sx and snapshot is not None else None

    def read(self, sx, table, lo=None, hi=None, hi_inclusive=False):
        """Remember that sx read table's primary key range [lo, hi) (lo and hi None: the whole table)."""
        if table in sx.reads and sx.reads[table] is None:
            return
        if lo is None and hi is None:
            sx.reads[table] = None
            return
        ranges = sx.reads.setdefault(table, [])
        if (lo, hi, hi_inclusive) not in ranges:
            ranges.append((lo, hi, hi_inclusive))
            if len(ranges) > RANGES_PER_TABLE:
                sx.reads[table] = None

    def saw(self, sx, versions, visible_at, table, key):
        """sx read a row: every version newer than the one it sees (or all, if none) was written by a
        transaction its snapshot does not see, and so was a deletion of the version it sees."""
        writers = {xmin for xmin, _, _ in versions[visible_at + 1:]}
        if visible_at >= 0 and versions[visible_at][1] is not None:
            writers.add(versions[visible_at][1])
        for xid in writers:
            writer = self.sx.get(xid)
            if writer is not None and writer is not sx:
                self._conflict(sx, writer, self.describe(table, key), current=sx)

    # ---- writes --------------------------------------------------------------------------------

    def wrote(self, txn, table, key):
        """txn wrote the row under key: every concurrent SERIALIZABLE reader of it gets a conflict out."""
        writer = self.sx.get(txn.xid)
        if writer is None:
            return
        for reader in list(self.sx.values()):
            if reader is writer or table not in reader.reads or not self._concurrent(writer, reader):
                continue
            ranges = reader.reads[table]
            if ranges is None:
                what = f"table {table}"
            elif any(_inside(key, r) for r in ranges):
                what = self.describe(table, key)
            else:
                continue
            self._conflict(reader, writer, what, current=writer)

    # ---- the dangerous structure ---------------------------------------------------------------

    def _conflict(self, reader, writer, what, current):
        if writer.xid in reader.out:
            return
        reader.out[writer.xid] = what
        writer.inn[reader.xid] = what
        self.emit("rw_conflict", **self.edge(reader.xid, writer.xid, what))
        pivot = reader if reader.inn else writer if writer.out else None
        if pivot is None:
            return
        into, (out_xid, out_what) = next(iter(pivot.inn.items())), next(iter(pivot.out.items()))
        error = SerializationError(
            "could not serialize access due to read/write dependencies among transactions: "
            f"{self._name(into[0])} read {into[1]} before {self._name(pivot.xid)} changed it, and "
            f"{self._name(pivot.xid)} read {out_what} before {self._name(out_xid)} changed it, so no serial "
            f"order fits; transaction {current.xid} was rolled back. Retry it.")
        self.emit("serialization_failure", session=current.owner, victim=current.xid, pivot=pivot.xid)
        error.rolled_back = current.txn
        error.dependencies = [self.edge(into[0], pivot.xid, into[1]), self.edge(pivot.xid, out_xid, out_what)]
        raise error

    def _concurrent(self, running, other):
        """Whether other overlaps running: running's snapshot does not see other's commit (or it has none yet,
        in which case it sees every commit made so far)."""
        if not other.committed:
            return True
        snapshot = running.txn.snapshot
        return snapshot is not None and not self.mvcc.sees(snapshot, other.xid)

    def _name(self, xid):
        sx = self.sx.get(xid)
        return f"transaction {xid} (session {sx.owner})" if sx else f"transaction {xid}"

    def edge(self, reader, writer, what):
        owner = lambda x: self.sx[x].owner if x in self.sx else None  # noqa: E731
        return {"reader": reader, "writer": writer, "what": what,
                "reader_session": owner(reader), "writer_session": owner(writer)}

    def edges(self):
        """Every rw-conflict still tracked, for drawing."""
        return [self.edge(s.xid, w, what) for s in self.sx.values() for w, what in s.out.items()]


def _inside(key, bounds):
    lo, hi, hi_inclusive = bounds
    if lo is not None and key < lo:
        return False
    return hi is None or key < hi or (hi_inclusive and key == hi)
