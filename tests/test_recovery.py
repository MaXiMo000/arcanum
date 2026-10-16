"""Crash recovery: committed data survives any crash, uncommitted data never appears.

Most tests run on CrashFS, which loses power at a chosen file operation and
then keeps a random subset of the unsynced writes, some of them torn. The
last test kills a real process that is writing to real files.
"""

import os
import random
import subprocess
import sys
import time

import pytest
from crashfs import CrashFS, PowerLoss

from arcanum import ConstraintError, Database
from arcanum.storage import Pager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- the log itself ----------------------------------------------------------------------


def test_commit_survives_power_loss_before_checkpoint():
    fs = CrashFS()
    pager = Pager("db", fs=fs)
    page = pager.allocate()
    pager.write(page, b"committed")
    pager.commit()  # in the log only; the data file has not been touched
    fs.arm(1)
    with pytest.raises(PowerLoss):
        pager.checkpoint()
    fs.power_loss(random.Random(1))
    pager = Pager("db", fs=fs)
    assert pager.recovered_frames == 2  # the page and the commit frame
    assert pager.read(page).rstrip(b"\x00") == b"committed"


def test_uncommitted_spilled_pages_are_discarded():
    fs = CrashFS()
    pager = Pager("db", fs=fs, pool_size=1)
    a, b = pager.allocate(), pager.allocate()
    pager.write(a, b"old a")
    pager.write(b, b"old b")
    pager.commit()
    pager.write(a, b"new a")
    pager.write(b, b"new b")  # evicts a: its new image goes to the log without a commit frame
    fs.arm(1)
    with pytest.raises(PowerLoss):
        pager.commit()
    fs.power_loss(random.Random(2), keep_everything=True)
    pager = Pager("db", fs=fs)
    assert pager.read(a).rstrip(b"\x00") == b"old a"
    assert pager.read(b).rstrip(b"\x00") == b"old b"


def test_torn_commit_frame_discards_the_transaction():
    fs = CrashFS()
    pager = Pager("db", fs=fs)
    page = pager.allocate()
    pager.write(page, b"first")
    pager.commit()
    pager.write(page, b"second")
    pager.commit()
    log = fs.files["db-wal"]
    del log.current[len(log.current) - 1000:]  # cut off the end of the last commit frame
    log.durable = bytearray(log.current)
    pager = Pager("db", fs=fs)
    assert pager.read(page).rstrip(b"\x00") == b"first"


def test_corrupted_frame_discards_the_transaction():
    fs = CrashFS()
    pager = Pager("db", fs=fs)
    page = pager.allocate()
    pager.write(page, b"first")
    pager.commit()
    log = fs.files["db-wal"]
    second_txn = len(log.current)
    pager.write(page, b"second")
    pager.commit()
    log.current[second_txn + 16 + 100] ^= 0xFF  # flip one bit pattern inside the page image
    log.durable = bytearray(log.current)
    pager = Pager("db", fs=fs)
    assert pager.recovered_frames == 2  # only the first transaction
    assert pager.read(page).rstrip(b"\x00") == b"first"


def test_rollback_removes_spilled_frames_from_the_log():
    fs = CrashFS()
    pager = Pager("db", fs=fs, pool_size=1)
    pages = [pager.allocate() for _ in range(3)]
    pager.commit()
    committed_size = len(fs.files["db-wal"].current)
    pager.savepoint()
    for p in pages:
        pager.write(p, b"discard me")  # each write evicts the previous page into the log
    assert len(fs.files["db-wal"].current) > committed_size
    pager.rollback()
    assert len(fs.files["db-wal"].current) == committed_size


def test_frames_from_an_older_log_are_ignored():
    fs = CrashFS()
    pager = Pager("db", fs=fs)
    page = pager.allocate()
    pager.write(page, b"v1")
    pager.commit()
    stale = bytes(fs.files["db-wal"].current)  # a log full of valid frames for v1
    pager.write(page, b"v2")
    pager.commit()
    pager.checkpoint()  # data file now has v2, and the log restarts with a new salt
    log = fs.files["db-wal"]
    log.current = bytearray(log.current) + stale[len(log.current):]  # old frames behind the new header
    log.durable = bytearray(log.current)
    pager = Pager("db", fs=fs)
    assert pager.recovered_frames == 0
    assert pager.read(page).rstrip(b"\x00") == b"v2"


# ---- a whole database under random crashes ----------------------------------------------------

FILLER = "x" * 150


def pad_sql(group, version):
    return f"'{group}:{version}:' || n || ':' || substr('{FILLER}', 1, ({7 * group} + {13 * version} + n) % 150)"


def pad(group, version, n):
    return f"{group}:{version}:{n}:" + FILLER[:(7 * group + 13 * version + n) % 150]


class Workload:
    """Random transactions against a model of what the database must contain.

    model: {group: (row count, version)}, plus the number of committed explicit
    transactions. `in_flight` is the transaction running when power is lost: it
    may or may not have committed, but never partially.
    """

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.groups = {}
        self.txns = 0
        self.next_group = 1
        self.in_flight = None

    def state(self):
        rows = {(g, n, pad(g, v, n)) for g, (count, v) in self.groups.items() for n in range(1, count + 1)}
        return rows, self.txns

    def after(self, txn):
        groups, txns = dict(self.groups), self.txns
        kind, g, value = txn
        if kind in ("insert", "autocommit"):
            groups[g] = (value, 0)
        elif kind == "update":
            groups[g] = (groups[g][0], value)
        elif kind == "delete":
            del groups[g]
        if kind in ("insert", "update", "delete"):
            txns += 1
        return groups, txns

    def choose(self):
        r = self.rng
        kind = r.choice(["insert", "insert", "update", "delete", "rollback", "autocommit"])
        if kind in ("update", "delete") and not self.groups:
            kind = "insert"
        if kind in ("insert", "autocommit"):
            g = self.next_group
            self.next_group += 1
            return kind, g, r.randint(1, 3) if kind == "autocommit" else r.randint(1, 25)
        if kind == "rollback":
            return kind, None, None
        g = r.choice(sorted(self.groups))
        return kind, g, self.groups[g][1] + 1 if kind == "update" else None

    def run(self, db, steps):
        for _ in range(steps):
            txn = self.choose()
            self.in_flight = txn
            self.execute(db, txn)
            self.groups, self.txns = self.after(txn)
            self.in_flight = None

    def execute(self, db, txn):
        kind, g, value = txn
        if kind == "autocommit":
            values = ", ".join(f"({g}, {n}, '{pad(g, 0, n)}')" for n in range(1, value + 1))
            db.execute(f"INSERT INTO item VALUES {values}")
            return
        db.execute("BEGIN")
        if kind == "insert":
            for start in range(1, value + 1, 8):
                values = ", ".join(f"({g}, {n}, '{pad(g, 0, n)}')" for n in range(start, min(start + 8, value + 1)))
                db.execute(f"INSERT INTO item VALUES {values}")
            with pytest.raises(ConstraintError):  # a failing statement inside the transaction undoes only itself
                db.execute(f"INSERT INTO item VALUES ({g}, 1, 'duplicate')")
        elif kind == "update":
            db.execute(f"UPDATE item SET pad = {pad_sql(g, value)} WHERE grp = {g}")
        elif kind == "delete":
            db.execute(f"DELETE FROM item WHERE grp = {g}")
        else:
            db.execute("DELETE FROM item")
            db.execute("UPDATE meta SET txns = -1")
            db.execute("ROLLBACK")
            return
        db.execute("UPDATE meta SET txns = txns + 1")
        db.execute("COMMIT")

    def check(self, db):
        rows = set(db.execute("SELECT grp, n, pad FROM item").rows)
        (txns,), = db.execute("SELECT txns FROM meta").rows
        allowed = [self.state()]
        if self.in_flight is not None:
            groups, count = self.after(self.in_flight)
            saved = self.groups, self.txns
            self.groups, self.txns = groups, count
            allowed.append(self.state())
            self.groups, self.txns = saved
        assert (rows, txns) in allowed, "database is neither the last committed state nor the next one"
        if (rows, txns) != allowed[0]:  # the in-flight transaction had committed
            self.groups, self.txns = self.after(self.in_flight)
        self.in_flight = None
        verify_structure(db, len(rows))


def verify_structure(db, row_count):
    catalog = db.catalog
    catalog.tree.verify()
    for table in catalog.tables.values():
        rows = catalog.btree(table.root).verify()
        for index in table.indexes:
            assert catalog.btree(index.root).verify() == rows
    assert catalog.btree(catalog.tables["item"].root).verify() == row_count


def open_db(fs):
    return Database("db", fs=fs, pool_size=6, max_keys=4, checkpoint_frames=30)


@pytest.mark.parametrize("seed", range(60))
def test_random_crashes_never_lose_or_tear_transactions(seed):
    fs = CrashFS()
    rng = random.Random(seed)
    workload = Workload(seed)
    db = open_db(fs)
    db.execute_script("""
        CREATE TABLE item (grp INTEGER, n INTEGER, pad TEXT, PRIMARY KEY (grp, n));
        CREATE INDEX item_pad ON item (pad);
        CREATE TABLE meta (id INTEGER PRIMARY KEY, txns INTEGER);
        INSERT INTO meta VALUES (1, 0);
    """)
    db.close()
    for _ in range(4):
        fs.arm(rng.randint(1, 700))
        try:
            db = open_db(fs)  # recovery itself may be interrupted
            workload.run(db, steps=10)
            db.close()
            fs.arm(None)
        except PowerLoss:
            fs.power_loss(rng, keep_everything=rng.random() < 0.2)
        db = open_db(fs)
        workload.check(db)
        db.close()


# ---- a real process, killed --------------------------------------------------------------------

CHILD = """
import sys
sys.path.insert(0, sys.argv[2])
from arcanum import Database
db = Database(sys.argv[1], pool_size=8, max_keys=4, checkpoint_frames=40)
db.execute("CREATE TABLE IF NOT EXISTS item (grp INTEGER, n INTEGER, PRIMARY KEY (grp, n))")
db.execute("CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, txns INTEGER)")
if not db.execute("SELECT * FROM meta").rows:
    db.execute("INSERT INTO meta VALUES (1, 0)")
i = db.execute("SELECT txns FROM meta").rows[0][0]
while True:
    i += 1
    db.execute("BEGIN")
    db.execute(f"INSERT INTO item SELECT {i}, n FROM (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL "
               f"SELECT n + 1 FROM s WHERE n < {i % 13 + 1}) SELECT n FROM s) AS seq")
    db.execute(f"DELETE FROM item WHERE grp = {i - 5} AND n % 2 = 0")
    db.execute(f"UPDATE meta SET txns = {i}")
    db.execute("COMMIT")
    print(i, flush=True)
"""


def expected_rows(txns):
    rows = set()
    for i in range(1, txns + 1):
        rows |= {(i, n) for n in range(1, i % 13 + 2)}
        rows -= {(i - 5, n) for n in range(2, 15, 2)}
    return rows


def test_killed_process_recovers_every_acknowledged_commit(tmp_path):
    path = str(tmp_path / "killed.db")
    rng = random.Random(7)
    acknowledged = 0
    for _ in range(4):
        child = subprocess.Popen([sys.executable, "-c", CHILD, path, ROOT], stdout=subprocess.PIPE, text=True)
        first = child.stdout.readline()  # wait until the child is committing
        assert first, "child process failed to start"
        time.sleep(rng.uniform(0.05, 0.4))
        child.kill()
        output = first + child.stdout.read()
        child.wait()
        acknowledged = max([acknowledged] + [int(line) for line in output.split()])
        with Database(path) as db:
            (txns,), = db.execute("SELECT txns FROM meta").rows
            assert txns in (acknowledged, acknowledged + 1)  # the last one may have committed unacknowledged
            assert set(db.execute("SELECT grp, n FROM item").rows) == expected_rows(txns)
            verify_structure(db, len(expected_rows(txns)))
        acknowledged = txns
