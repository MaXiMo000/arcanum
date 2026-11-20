"""Multi-version concurrency control, the way PostgreSQL does it.

A table row is a chain of versions, oldest first. Each version carries xmin,
the transaction that created it, and xmax, the one that deleted it (None while
it is current). An UPDATE deletes the old version and appends a new one, so a
reader still holding an older snapshot keeps seeing the old one.

A snapshot fixes which transactions a statement can see: every transaction
that committed before the snapshot was taken, and its own. xid 0 is the frozen
transaction: a version whose creator is visible to every possible snapshot is
frozen, and then needs no commit-log lookup at all.

Savepoints are subtransactions, as in PostgreSQL: each SAVEPOINT takes a new
transaction id, the transaction writes with its newest one, and every id it
owns counts as its own. ROLLBACK TO aborts the savepoint's ids, so the versions
they created disappear and the rows they deleted come back, for everyone,
without touching a page.
"""

from dataclasses import dataclass, field

FROZEN = 0
READ_COMMITTED = "READ COMMITTED"
REPEATABLE_READ = "REPEATABLE READ"
XID_BATCH = 1000  # transaction ids reserved on disk at a time, so they are never reused after a crash


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What a statement sees. xmin: every transaction below it had finished. xmax: the first transaction
    id not yet handed out. active: transactions still running when it was taken. own: the reader's own
    transaction ids, its savepoints' included (empty for a read-only statement outside a transaction).
    top: the reader's transaction id."""
    xmin: int
    xmax: int
    active: frozenset
    own: frozenset = frozenset()
    top: int | None = None


@dataclass(eq=False)
class Transaction:
    xid: int
    isolation: str
    owner: str  # the session (connection) running it
    snapshot: Snapshot | None = None  # REPEATABLE READ: the one taken by the first statement
    touched: dict = field(default_factory=dict)  # table name -> keys of the chains it changed
    own: set = field(default_factory=set)  # its id and its savepoints' ids that were not rolled back
    current: int = 0  # the id new versions are stamped with: the innermost savepoint's, or xid

    def __post_init__(self):
        self.own.add(self.xid)
        self.current = self.current or self.xid


class TransactionManager:
    """Hands out transaction ids, tracks which are running, committed or aborted, and takes snapshots.

    committed holds the commit log: committed ids that rows may still mention (ids whose every version
    was frozen at commit are not needed). An id that is neither running nor committed was aborted,
    including any that were running when the process died.
    """

    def __init__(self, committed=(), next_xid=1):
        self.committed = set(committed)
        self.running = {}  # xid -> Transaction
        self.next_xid = max(next_xid, 1)

    def begin(self, owner, isolation=READ_COMMITTED):
        xid = self.next_xid
        self.next_xid += 1
        txn = Transaction(xid, isolation, owner)
        self.running[xid] = txn
        return txn

    def subtransaction(self, txn):
        """Start a savepoint of txn: a new id it writes with from now on."""
        xid = self.next_xid
        self.next_xid += 1
        self.running[xid] = txn
        txn.own.add(xid)
        txn.current = xid
        return xid

    def abort(self, txn, xids):
        """Roll back savepoint ids of txn: what they wrote is invisible from now on."""
        for xid in xids:
            self.running.pop(xid, None)
            txn.own.discard(xid)

    def end(self, txn, committed):
        for xid in txn.own:
            self.running.pop(xid, None)
        if committed:
            self.committed.update(txn.own)

    def status(self, xid):
        """"committed", "running" or "aborted"."""
        if xid == FROZEN or xid in self.committed:
            return "committed"
        return "running" if xid in self.running else "aborted"

    def snapshot(self, txn=None):
        own = frozenset(txn.own) if txn is not None else frozenset()
        active = frozenset(x for x in self.running if x not in own)
        return Snapshot(min(active, default=self.next_xid), self.next_xid, active, own,
                        txn.xid if txn is not None else None)

    def horizon(self, exclude=None):
        """Every committed transaction below this is visible to every snapshot still in use (other than
        exclude's), so versions it created can be frozen and versions it deleted dropped."""
        held = [t.snapshot.xmin for t in self.running.values() if t.snapshot is not None and t is not exclude]
        return min(held, default=self.next_xid)

    # ---- visibility ----------------------------------------------------------------------------

    def sees(self, snap, xid):
        """Whether the effects of transaction xid are visible to snapshot snap."""
        if xid == FROZEN or xid in snap.own:
            return True
        return xid < snap.xmax and xid not in snap.active and self.status(xid) == "committed"

    def visible(self, snap, xmin, xmax):
        """Whether a version created by xmin and deleted by xmax (or None) is visible to snap."""
        if not self.sees(snap, xmin):
            return False
        return xmax is None or not self.sees(snap, xmax)


# ---- version chains --------------------------------------------------------------------------


def unpack(values, width):
    """A stored chain (flat tuple) as a list of [xmin, xmax, row] versions, oldest first."""
    step = width + 2
    return [[values[i], values[i + 1], tuple(values[i + 2:i + step])] for i in range(0, len(values), step)]


def pack(versions):
    flat = []
    for xmin, xmax, row in versions:
        flat.append(xmin)
        flat.append(xmax)
        flat.extend(row)
    return tuple(flat)


def prune(versions, manager, horizon):
    """Drop versions nobody can see any more and freeze the rest where possible.

    Removes versions created by aborted transactions and versions whose deletion committed below the
    horizon; forgets deletions by aborted transactions; freezes creators that committed below it.
    Returns the surviving versions (possibly none: the row is gone for everyone).
    """
    kept = []
    for xmin, xmax, row in versions:
        created = manager.status(xmin)
        if created == "aborted":
            continue
        if xmax is not None:
            deleted = manager.status(xmax)
            if deleted == "aborted":
                xmax = None
            elif deleted == "committed" and xmax < horizon:
                continue
        if created == "committed" and xmin != FROZEN and xmin < horizon:
            xmin = FROZEN
        kept.append([xmin, xmax, row])
    return kept


def settled(versions):
    """A single frozen, undeleted version: visible to everyone, so readers can skip the version check."""
    return len(versions) == 1 and versions[0][0] == FROZEN and versions[0][1] is None
