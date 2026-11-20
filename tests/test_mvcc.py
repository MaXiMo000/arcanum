"""Concurrent sessions: PostgreSQL-style row versions, snapshots and write conflicts.

Sessions take turns (one statement at a time), so where PostgreSQL would make
a statement wait for another transaction, Arcanum fails it with LockWaitError.
"""

import random

import pytest
from crashfs import CrashFS

from arcanum import ConstraintError, Database, SqlError
from arcanum.errors import LockWaitError, SerializationError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE account (id INTEGER PRIMARY KEY, owner TEXT UNIQUE, balance INTEGER);
            CREATE INDEX account_balance ON account (balance);
            INSERT INTO account SELECT n, 'owner ' || n, n * 100 FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 20) SELECT n FROM s) AS q;
        """)
        yield database


def value(conn, sql):
    return conn.execute(sql).rows[0][0]


def test_no_dirty_reads_and_read_committed_sees_commits(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE account SET balance = 0 WHERE id = 1")
    assert value(a, "SELECT balance FROM account WHERE id = 1") == 0  # its own change
    assert value(b, "SELECT balance FROM account WHERE id = 1") == 100  # not someone else's
    b.execute("BEGIN")  # READ COMMITTED: a new snapshot per statement
    assert value(b, "SELECT balance FROM account WHERE id = 1") == 100
    a.execute("COMMIT")
    assert value(b, "SELECT balance FROM account WHERE id = 1") == 0  # a non-repeatable read, as in PostgreSQL
    b.execute("COMMIT")


def test_repeatable_read_keeps_its_snapshot_and_refuses_lost_updates(db):
    a, b = db.connect("a"), db.connect("b")
    b.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert value(b, "SELECT sum(balance) FROM account") == 21000
    a.execute("UPDATE account SET balance = balance + 1 WHERE id = 2")
    a.execute("INSERT INTO account VALUES (99, 'new', 5)")
    assert value(b, "SELECT sum(balance) FROM account") == 21000  # same snapshot: no change, no phantom
    assert value(b, "SELECT count(*) FROM account WHERE id = 99") == 0
    with pytest.raises(SerializationError, match="could not serialize access"):
        b.execute("UPDATE account SET balance = balance - 50 WHERE id = 2")
    b.execute("UPDATE account SET balance = balance - 50 WHERE id = 3")  # untouched rows are fine
    b.execute("COMMIT")
    assert db.execute("SELECT id, balance FROM account WHERE id IN (2, 3) ORDER BY id").rows == [(2, 201), (3, 250)]


def test_writers_wait_for_writers_but_readers_never_do(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE account SET balance = 1 WHERE id = 5")
    with pytest.raises(LockWaitError, match="session a"):
        b.execute("UPDATE account SET balance = 2 WHERE id = 5")
    with pytest.raises(LockWaitError):
        b.execute("DELETE FROM account WHERE balance >= 500")  # reaches row 5 too
    b.execute("UPDATE account SET balance = 2 WHERE id = 6")  # other rows are free
    assert value(b, "SELECT balance FROM account WHERE id = 5") == 500  # reading never waits
    a.execute("COMMIT")
    b.execute("UPDATE account SET balance = balance + 1 WHERE id = 5")  # no lost update: builds on a's commit
    assert value(db, "SELECT balance FROM account WHERE id = 5") == 2


def test_key_and_unique_conflicts_follow_the_other_transaction(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("INSERT INTO account VALUES (50, 'fifty', 1)")
    with pytest.raises(LockWaitError):
        b.execute("INSERT INTO account VALUES (50, 'other', 1)")
    with pytest.raises(LockWaitError):
        b.execute("INSERT INTO account VALUES (51, 'fifty', 1)")  # the unique owner, too
    a.execute("ROLLBACK")
    b.execute("INSERT INTO account VALUES (50, 'fifty', 1)")  # free again

    a.execute("BEGIN")
    a.execute("DELETE FROM account WHERE id = 7")
    with pytest.raises(LockWaitError):
        b.execute("INSERT INTO account VALUES (7, 'seven again', 1)")
    a.execute("COMMIT")
    b.execute("INSERT INTO account VALUES (7, 'seven again', 1)")
    with pytest.raises(ConstraintError):
        b.execute("INSERT INTO account VALUES (7, 'dup', 1)")


def test_indexes_see_the_right_version(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE account SET balance = 12345, owner = 'renamed' WHERE id = 4")
    # b reaches the row through both old and new index entries and must see only the old version.
    assert b.execute("SELECT id FROM account WHERE balance = 400").rows == [(4,)]
    assert b.execute("SELECT id FROM account WHERE balance = 12345").rows == []
    assert b.execute("SELECT id, balance FROM account WHERE balance > 1000 ORDER BY balance").rows[-1] == (20, 2000)
    assert a.execute("SELECT id FROM account WHERE balance = 12345").rows == [(4,)]
    assert a.execute("SELECT id FROM account WHERE balance = 400").rows == []
    a.execute("ROLLBACK")
    assert db.execute("SELECT owner FROM account WHERE id = 4").rows == [("owner 4",)]


def test_finished_transactions_leave_single_frozen_versions_and_no_commit_log(db):
    a, b = db.connect("a"), db.connect("b")
    b.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    b.execute("SELECT count(*) FROM account")
    a.execute("UPDATE account SET balance = 0 WHERE id <= 3")
    assert db.catalog.unsettled  # b's snapshot still needs the old versions
    assert db.catalog.clog  # and a's id stays in the commit log while versions mention it
    b.execute("COMMIT")
    a.execute("DELETE FROM account WHERE id = 20")
    assert not db.catalog.unsettled and not db.catalog.clog
    table = db.catalog.tables["account"]
    for key, _ in db.catalog.btree(table.root).scan():
        assert db.catalog.chain(table, key)[0][:2] == [0, None]
    # Index-only scans are back to answering from the index alone, with old entries gone.
    assert db.execute("SELECT id FROM account WHERE balance = 0 ORDER BY id").rows == [(1,), (2,), (3,)]


def test_schema_changes_in_a_transaction_hold_off_other_sessions(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE account SET balance = 0 WHERE id = 1")
    a.execute("CREATE TABLE scratch (x INTEGER)")
    with pytest.raises(LockWaitError, match="changed the schema"):
        b.execute("SELECT count(*) FROM account")
    a.execute("ROLLBACK")
    assert "scratch" not in db.catalog.tables
    assert value(b, "SELECT balance FROM account WHERE id = 1") == 100
    b.execute("BEGIN")
    with pytest.raises(LockWaitError, match="session b"):
        a.execute("DROP TABLE account")
    b.execute("ROLLBACK")


def test_a_crash_drops_unfinished_transactions_of_every_session():
    fs = CrashFS()
    db = Database("db", fs=fs, max_keys=4)
    db.execute_script("""
        CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER);
        CREATE INDEX t_v ON t (v);
        INSERT INTO t VALUES (1, 10), (2, 20), (3, 30);
    """)
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE t SET v = 11 WHERE id = 1")
    a.execute("INSERT INTO t VALUES (4, 40)")
    b.execute("UPDATE t SET v = 22 WHERE id = 2")  # commits, and with it the log holds a's open versions
    fs.power_loss(random.Random(0), keep_everything=True)
    db = Database("db", fs=fs, max_keys=4)
    assert db.execute("SELECT id, v FROM t ORDER BY id").rows == [(1, 10), (2, 22), (3, 30)]
    assert db.execute("SELECT id FROM t WHERE v = 11").rows == []  # no stale index entry either
    assert not db.catalog.unsettled
    db.execute("INSERT INTO t VALUES (4, 41)")  # the id a held is free


# ---- SERIALIZABLE (serializable snapshot isolation) ----------------------------------------


def serializable(db, *names):
    conns = [db.connect(n) for n in names]
    for c in conns:
        c.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
    return conns


def test_serializable_refuses_write_skew_and_rolls_back(db):
    a, b = serializable(db, "a", "b")
    # Each checks the total stays at least 1000, then withdraws from a different account.
    assert value(a, "SELECT sum(balance) FROM account WHERE id <= 2") == 300
    assert value(b, "SELECT sum(balance) FROM account WHERE id <= 2") == 300
    a.execute("UPDATE account SET balance = balance - 300 WHERE id = 1")
    with pytest.raises(SerializationError, match="read/write dependencies") as caught:
        b.execute("UPDATE account SET balance = balance - 300 WHERE id = 2")
    assert [d["what"] for d in caught.value.dependencies] == ["row (id = 1)", "row (id = 2)"]
    assert b.txn is None  # the whole transaction was rolled back, as in PostgreSQL
    a.execute("COMMIT")
    assert db.execute("SELECT id, balance FROM account WHERE id <= 2 ORDER BY id").rows == [(1, -200), (2, 200)]
    assert db.ssi.sx == {}  # nothing left to remember once nobody is concurrent


def test_serializable_lets_independent_work_commit(db):
    a, b = serializable(db, "a", "b")
    assert value(a, "SELECT balance FROM account WHERE id = 1") == 100
    assert value(b, "SELECT balance FROM account WHERE id BETWEEN 5 AND 6") == 500
    a.execute("UPDATE account SET balance = 0 WHERE id = 1")
    b.execute("UPDATE account SET balance = 0 WHERE id = 6")
    a.execute("COMMIT")
    b.execute("COMMIT")


def test_serializable_sees_conflicts_through_a_committed_reader(db):
    # a reads what b then writes (a -> b); b commits; a then writes what c read before b's commit.
    a, b, c = serializable(db, "a", "b", "c")
    value(c, "SELECT balance FROM account WHERE id = 3")
    value(a, "SELECT balance FROM account WHERE id = 2")
    b.execute("UPDATE account SET balance = 1 WHERE id = 2")  # a -> b
    b.execute("COMMIT")
    a.execute("UPDATE account SET balance = 1 WHERE id = 4")  # unrelated: fine
    with pytest.raises(SerializationError, match="read/write dependencies"):
        a.execute("UPDATE account SET balance = 1 WHERE id = 3")  # c -> a makes a a pivot
    c.execute("COMMIT")


def test_serializable_catches_phantoms_in_key_ranges(db):
    a, b = serializable(db, "a", "b")
    assert value(a, "SELECT count(*) FROM account WHERE id BETWEEN 30 AND 40") == 0
    assert value(b, "SELECT count(*) FROM account WHERE id BETWEEN 50 AND 60") == 0
    b.execute("INSERT INTO account VALUES (35, 'b', 1)")  # into a's range: a -> b
    with pytest.raises(SerializationError):
        a.execute("INSERT INTO account VALUES (55, 'a', 1)")  # into b's range: b -> a
    b.execute("COMMIT")


def test_other_levels_are_not_tracked(db):
    a = db.connect("a")
    a.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    value(a, "SELECT sum(balance) FROM account")
    assert db.ssi.sx == {}
    a.execute("COMMIT")


# ---- savepoints (subtransactions) ----------------------------------------------------------


def test_savepoints_undo_part_of_a_transaction(db):
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("UPDATE account SET balance = 1 WHERE id = 1")
    a.execute("SAVEPOINT s1")
    a.execute("UPDATE account SET balance = 2 WHERE id = 1")
    a.execute("DELETE FROM account WHERE id = 2")
    a.execute("INSERT INTO account VALUES (50, 'fifty', 5)")
    a.execute("SAVEPOINT s2")
    a.execute("UPDATE account SET balance = 3 WHERE id = 3")
    a.execute("RELEASE SAVEPOINT s2")  # s2's change now belongs to s1
    assert value(a, "SELECT balance FROM account WHERE id = 1") == 2
    a.execute("ROLLBACK TO SAVEPOINT s1")
    assert a.execute("SELECT id, balance FROM account WHERE id IN (1, 2, 3, 50) ORDER BY id").rows == [
        (1, 1), (2, 200), (3, 300)]
    with pytest.raises(LockWaitError):  # row 1 is still locked by the outer transaction
        b.execute("UPDATE account SET balance = 9 WHERE id = 1")
    b.execute("UPDATE account SET balance = 9 WHERE id = 2")  # row 2 was released by the rollback
    a.execute("UPDATE account SET balance = 4 WHERE id = 3")  # the savepoint is still there, with a new id
    a.execute("ROLLBACK TO s1")
    a.execute("COMMIT")
    assert db.execute("SELECT id, balance FROM account WHERE id <= 3 ORDER BY id").rows == [(1, 1), (2, 9), (3, 300)]
    assert db.execute("SELECT count(*) FROM account WHERE id = 50").rows == [(0,)]


def test_savepoint_errors_and_repeatable_read(db):
    a = db.connect("a")
    with pytest.raises(SqlError, match="transaction block"):
        a.execute("SAVEPOINT x")
    a.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert value(a, "SELECT balance FROM account WHERE id = 4") == 400
    a.execute("SAVEPOINT x")
    a.execute("UPDATE account SET balance = 0 WHERE id = 4")
    assert value(a, "SELECT balance FROM account WHERE id = 4") == 0  # its own savepoint's write
    with pytest.raises(SqlError, match="savepoint nope does not exist"):
        a.execute("ROLLBACK TO nope")
    a.execute("ROLLBACK TO x")
    assert value(a, "SELECT balance FROM account WHERE id = 4") == 400
    a.execute("COMMIT")
