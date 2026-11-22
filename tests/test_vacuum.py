"""VACUUM: what it can and cannot remove, VACUUM FULL's compact rewrite, and the bulk load beneath it."""

import random

import pytest

from arcanum import Database
from arcanum.errors import LockWaitError, SqlError
from arcanum.storage import BTree, Pager, encode
from arcanum.storage.files import CountingMemFileSystem


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "v.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE acct (id INTEGER PRIMARY KEY, balance INTEGER, note TEXT);
            CREATE INDEX acct_balance ON acct (balance);
            INSERT INTO acct SELECT n, n % 7, 'x' FROM generate_series(1, 200) AS n;
        """)
        yield database


def report(db, sql):
    result = db.execute(sql)
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def verify(db):
    for table in db.catalog.tables.values():
        for root in [table.root, *(i.root for i in table.indexes)]:
            db.catalog.btree(root).verify()


def test_old_versions_stay_while_a_snapshot_needs_them(db):
    reader = db.connect("B")
    reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert reader.execute("SELECT sum(balance) FROM acct").rows == [(sum(n % 7 for n in range(1, 201)),)]
    for i in range(3):
        db.execute(f"UPDATE acct SET balance = balance + 1, note = 'update {i}'")
    (held,) = report(db, "VACUUM acct")
    assert held["old_versions_kept"] == 600 and held["kept_for"].startswith("session B's transaction ")
    reader.execute("COMMIT")  # the snapshot is gone: committing prunes what it held
    (free,) = report(db, "VACUUM acct")
    assert free["old_versions_kept"] == 0 and free["kept_for"] is None
    assert free["bytes_after"] < held["bytes_after"] / 2  # four versions per row became one
    verify(db)


def test_vacuum_full_rewrites_tables_and_indexes_compactly(db, tmp_path):
    db.execute("INSERT INTO acct VALUES (500, 1, repeat('big', 4000))")  # an overflow (TOAST) value
    db.execute("DELETE FROM acct WHERE id % 3 <> 0 AND id < 500")
    (done,) = report(db, "VACUUM FULL acct")
    assert done["pages_after"] < 0.7 * done["pages_before"] and done["rows"] == 67
    verify(db)
    expected = db.execute("SELECT id, balance, length(note) FROM acct ORDER BY id").rows
    assert db.execute("SELECT count(*) FROM acct WHERE balance = 3").rows == [(sum(1 for r in expected if r[1] == 3),)]
    db.execute("INSERT INTO acct SELECT n, 2, 'later' FROM generate_series(1000, 1050) AS n")  # still a working tree
    verify(db)
    db.close()
    with Database(tmp_path / "v.db", max_keys=4) as reopened:
        rows = reopened.execute("SELECT id, balance, length(note) FROM acct WHERE id < 1000 ORDER BY id").rows
        assert rows == expected and (500, 1, 12000) in rows


def test_vacuum_is_refused_where_postgresql_refuses_it(db):
    other = db.connect("B")
    other.execute("BEGIN")
    with pytest.raises(SqlError, match="inside a transaction block"):
        other.execute("VACUUM")
    with pytest.raises(LockWaitError):
        db.execute("VACUUM FULL")  # it rewrites tables: it waits for open transactions, like a schema change
    other.execute("ROLLBACK")
    assert [r["table"] for r in report(db, "VACUUM")] == ["acct"]
    with pytest.raises(SqlError, match="nope"):
        db.execute("VACUUM nope")


@pytest.mark.parametrize(("max_keys", "count"), [(None, 0), (None, 1), (None, 3000), (4, 1), (4, 2), (4, 3), (4, 500),
                                                 (5, 77)])
def test_a_bulk_loaded_tree_is_valid_dense_and_writable(max_keys, count):
    items = [(encode((i,)), b"v" * (i % 40)) for i in range(count)]
    tree = BTree.bulk_load(Pager("b", pool_size=10**6, fs=CountingMemFileSystem()), items, max_keys)
    tree.verify()
    assert list(tree.scan()) == items
    inserted = BTree.create(Pager("i", pool_size=10**6, fs=CountingMemFileSystem()), max_keys)
    for key, value in items:
        inserted.insert(key, value)
    assert len(tree.dump()) <= len(inserted.dump())
    rng = random.Random(count)
    for i in range(count, count + 40):
        tree.insert(encode((i,)), b"new")
    for i in rng.sample(range(count + 40), min(count + 40, 30)):
        tree.delete(encode((i,)))
    tree.verify()
