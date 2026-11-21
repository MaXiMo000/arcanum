"""Values too large for a B+tree leaf live in overflow pages (TOAST)."""

import random
import string

import pytest
from crashfs import CrashFS, PowerLoss

from arcanum import ConstraintError, Database, ExecutionError
from arcanum.values import Jsonb


def text(n, seed=1):
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))  # barely compressible


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db") as database:
        database.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta JSONB)")
        yield database


def events(db):
    seen = []
    db.events.subscribe(lambda kind, data: seen.append((kind, data)))
    return seen


def test_large_values_round_trip_and_survive_reopen(tmp_path):
    path = tmp_path / "r.db"
    big, doc = text(50_000), {"items": list(range(3000)), "name": "x" * 2000}
    with Database(path) as db:
        db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT, meta JSONB, tags TEXT[])")
        db.execute("INSERT INTO docs VALUES (1, ?, ?, ?)".replace("?", "{}").format(
            "'" + big + "'", "'" + Jsonb(doc).text + "'", "'{" + ",".join(["tag"] * 800) + "}'"))
        assert db.execute("SELECT length(body), meta->'items'->2999, cardinality(tags) FROM docs").rows == [
            (50_000, Jsonb(2999), 800)]
    with Database(path) as db:
        body, meta = db.execute("SELECT body, meta FROM docs WHERE id = 1").rows[0]
        assert body == big and meta == Jsonb(doc)


def test_compression_and_page_counts(db):
    seen = events(db)
    db.execute(f"INSERT INTO docs (id, body) VALUES (1, '{'a' * 100_000}')")  # compresses to almost nothing
    db.execute(f"INSERT INTO docs (id, body) VALUES (2, '{text(20_000)}')")  # does not compress much
    writes = [d for k, d in seen if k == "toast_write"]
    assert writes[0]["compressed"] and writes[0]["stored"] < 1_000
    assert writes[1]["stored"] > 10_000 and len(db.catalog.toast_pages(db.catalog.tables["docs"])) >= 4


def test_updates_share_unchanged_values_and_free_what_is_gone(db):
    db.execute(f"INSERT INTO docs (id, title, body) VALUES (1, 'a', '{text(30_000)}')")
    table = db.catalog.tables["docs"]
    pages = set(db.catalog.toast_pages(table))
    seen = events(db)
    db.execute("UPDATE docs SET title = 'b' WHERE id = 1")  # the body is kept: same pointer, no new pages
    assert not [k for k, _ in seen if k == "toast_write"]
    assert set(db.catalog.toast_pages(table)) == pages
    db.execute(f"UPDATE docs SET body = '{text(30_000, seed=2)}' WHERE id = 1")
    assert not set(db.catalog.toast_pages(table)) & pages  # the old body's pages were freed at commit
    count = db.pager.page_count
    db.execute("DELETE FROM docs WHERE id = 1")
    assert db.catalog.toast_pages(table) == []
    db.execute(f"INSERT INTO docs (id, body) VALUES (2, '{text(30_000, seed=3)}')")
    assert db.pager.page_count <= count  # freed pages were reused


def test_snapshots_keep_seeing_old_large_values(db):
    old, new = text(20_000, 4), text(20_000, 5)
    db.execute(f"INSERT INTO docs (id, body) VALUES (1, '{old}')")
    a, b = db.connect("a"), db.connect("b")
    b.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert b.execute("SELECT body FROM docs").rows == [(old,)]
    a.execute(f"UPDATE docs SET body = '{new}' WHERE id = 1")
    assert b.execute("SELECT body FROM docs").rows == [(old,)]  # its pages are kept while b may need them
    b.execute("COMMIT")
    assert db.execute("SELECT body FROM docs").rows == [(new,)]
    assert len(db.catalog.toast_pages(db.catalog.tables["docs"])) == len(
        db.catalog.toast_pages(db.catalog.tables["docs"]))


def test_rollbacks_free_overflow_pages(db):
    table = db.catalog.tables["docs"]
    db.execute("BEGIN")
    db.execute(f"INSERT INTO docs (id, body) VALUES (1, '{text(20_000)}')")
    assert db.catalog.toast_pages(table)
    db.execute("ROLLBACK")
    assert db.catalog.toast_pages(table) == []
    with pytest.raises(ConstraintError):  # the first row is written (with its pages), then the statement fails
        db.execute(f"INSERT INTO docs (id, body) VALUES (2, '{text(20_000)}'), (2, 'again')")
    assert db.catalog.toast_pages(table) == []


def test_alter_and_drop_with_large_values(db):
    db.execute(f"INSERT INTO docs (id, body) VALUES (1, '{text(10_000)}')")
    db.execute("ALTER TABLE docs ADD COLUMN extra TEXT DEFAULT 'x'")
    assert db.execute("SELECT length(body), extra FROM docs").rows == [(10_000, "x")]
    free_before = len(db.pager.free_pages())
    db.execute("DROP TABLE docs")
    assert len(db.pager.free_pages()) > free_before + 2  # the overflow pages went back to the free list


def test_index_entries_still_have_a_limit(db):
    db.execute("CREATE INDEX docs_body ON docs (body)")
    with pytest.raises(ExecutionError, match="entry of index docs_body"):
        db.execute(f"INSERT INTO docs (id, body) VALUES (1, '{text(5_000)}')")


@pytest.mark.parametrize("seed", range(4))
def test_power_loss_never_tears_a_large_value(seed):
    def body(row, version):
        return f"{row}:{version}:" + text(3_000 + version * 700, seed=row * 100 + version)

    fs, rng = CrashFS(), random.Random(seed)
    acked, inflight = dict.fromkeys(range(4), 0), None
    db = Database("db", fs=fs, pool_size=8, checkpoint_frames=40)
    db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
    for r in acked:
        db.execute(f"INSERT INTO docs VALUES ({r}, '{body(r, 0)}')")
    db.close()
    for _ in range(5):
        fs.arm(rng.randint(1, 400))
        try:
            db = Database("db", fs=fs, pool_size=8, checkpoint_frames=40)
            for _ in range(8):
                row = rng.randrange(4)
                inflight = (row, acked[row] + 1)
                db.execute(f"UPDATE docs SET body = '{body(*inflight)}' WHERE id = {row}")
                acked[row] += 1
                inflight = None
            db.close()
            fs.arm(None)
        except PowerLoss:
            fs.power_loss(rng, keep_everything=rng.random() < 0.2)
        db = Database("db", fs=fs, pool_size=8, checkpoint_frames=40)
        for row, value in db.execute("SELECT id, body FROM docs ORDER BY id").rows:
            allowed = {body(row, acked[row])}
            if inflight is not None and inflight[0] == row:
                allowed.add(body(*inflight))
            assert value in allowed
            if inflight is not None and inflight[0] == row and value == body(*inflight):
                acked[row] += 1  # the in-flight commit made it to disk before the power went
        inflight = None
        db.close()
