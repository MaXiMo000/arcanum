"""Sort-order-aware plans: index order instead of a Sort, backward scans, LIMIT, merge joins."""

import random

import pytest

from arcanum import Database


def plan(db, sql):
    return "\n".join(r[0] for r in db.execute("EXPLAIN " + sql).rows)


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "o.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER NOT NULL, score INTEGER, name TEXT);
            CREATE INDEX t_grp ON t (grp);
            CREATE INDEX t_score ON t (score);
        """)
        rng = random.Random(3)
        values = ", ".join(f"({i}, {rng.randrange(10)}, {'NULL' if i % 7 == 0 else rng.randrange(100)}, 'n{i}')"
                           for i in range(1, 301))
        database.execute(f"INSERT INTO t VALUES {values}")
        database.execute("ANALYZE")
        yield database


def test_primary_key_order_needs_no_sort(db):
    assert "Sort" not in plan(db, "SELECT id, name FROM t ORDER BY id")
    assert [r[0] for r in db.execute("SELECT id FROM t ORDER BY id").rows] == list(range(1, 301))
    backward = plan(db, "SELECT id FROM t ORDER BY id DESC LIMIT 3")
    assert "Sort" not in backward and "Backward" in backward
    assert db.execute("SELECT id FROM t ORDER BY id DESC LIMIT 3").rows == [(300,), (299,), (298,)]
    ranged = "SELECT id FROM t WHERE id BETWEEN 20 AND 25 ORDER BY id DESC"
    assert "Sort" not in plan(db, ranged)
    assert [r[0] for r in db.execute(ranged).rows] == [25, 24, 23, 22, 21, 20]


def test_index_order_respects_nulls(db):
    # The index keeps NULLs first; ORDER BY score puts them last (as PostgreSQL does), so a sort is still needed
    # unless the query asks for NULLS FIRST or the range excludes NULLs.
    assert "Sort" in plan(db, "SELECT score FROM t WHERE score >= 0 OR score IS NULL ORDER BY score")
    for sql in ("SELECT id, score FROM t WHERE score > 50 ORDER BY score, id",
                "SELECT id, score FROM t ORDER BY score NULLS FIRST, id"):
        expected = sorted(db.execute(sql.split(" ORDER BY")[0]).rows,
                          key=lambda r: (r[1] is not None, r[1] if r[1] is not None else 0, r[0]))
        assert db.execute(sql).rows == expected
    assert "Sort" not in plan(db, "SELECT id, score FROM t WHERE score > 50 ORDER BY score, id")


def test_limit_reads_an_index_in_order(db):
    sql = "SELECT id, grp FROM t ORDER BY grp DESC, id DESC LIMIT 4"
    text = plan(db, sql)
    assert "Sort" not in text and "t_grp" in text
    expected = sorted(db.execute("SELECT id, grp FROM t").rows, key=lambda r: (r[1], r[0]), reverse=True)[:4]
    assert db.execute(sql).rows == expected
    assert "Sort" in plan(db, "SELECT id, grp FROM t ORDER BY grp, id")  # no LIMIT: scanning and sorting wins


def test_merge_joins(tmp_path):
    with Database(tmp_path / "m.db", max_keys=4) as db:
        db.execute_script("""
            CREATE TABLE a (id INTEGER PRIMARY KEY, x TEXT);
            CREATE TABLE b (id INTEGER PRIMARY KEY, y TEXT);
            INSERT INTO a SELECT n, 'a' || n FROM generate_series(1, 60) AS n;
            INSERT INTO b SELECT n, 'b' || n FROM generate_series(30, 90, 2) AS n;
            ANALYZE;
        """)
        sql = "SELECT a.id, b.y FROM a JOIN b ON a.id = b.id ORDER BY a.id"
        text = plan(db, sql)
        assert "MergeJoin" in text and "Sort" not in text
        assert db.execute(sql).rows == [(n, f"b{n}") for n in range(30, 61, 2)]
        left = db.execute("SELECT a.id, b.y FROM a LEFT JOIN b ON a.id = b.id WHERE a.id BETWEEN 28 AND 32").rows
        assert left == [(28, None), (29, None), (30, "b30"), (31, None), (32, "b32")]
        dupes = """SELECT x.k, x.v, y.w FROM (SELECT id % 5 AS k, id AS v FROM a ORDER BY 1) x
                   JOIN (SELECT id % 5 AS k, id AS w FROM b ORDER BY 1) y ON x.k = y.k"""
        assert "MergeJoin" in plan(db, dupes)
        expected = sorted((v % 5, v, w) for v in range(1, 61) for w in range(30, 91, 2) if v % 5 == w % 5)
        assert sorted(db.execute(dupes).rows) == expected


@pytest.mark.parametrize("seed", range(6))
def test_order_matches_the_unoptimized_engine(tmp_path, seed):
    rng = random.Random(seed)
    setup = """CREATE TABLE r (id INTEGER PRIMARY KEY, a INTEGER, b TEXT);
               CREATE INDEX r_a ON r (a); CREATE INDEX r_ab ON r (a, b);"""
    rows = ", ".join(f"({i}, {'NULL' if rng.random() < 0.2 else rng.randrange(8)}, "
                     f"{'NULL' if rng.random() < 0.2 else repr('s' + str(rng.randrange(5)))})" for i in range(1, 120))
    queries = []
    for _ in range(25):
        column = rng.choice(["id", "a", "b", "a, b", "a, id"])
        direction = rng.choice(["", " DESC"])
        nulls = rng.choice(["", " NULLS FIRST", " NULLS LAST"])
        where = rng.choice(["", " WHERE a > 3", " WHERE a = 2", " WHERE id BETWEEN 10 AND 40", " WHERE a IN (1, 5)"])
        limit = rng.choice(["", " LIMIT 5", " LIMIT 7 OFFSET 3"])
        keys = ", ".join(f"{c.strip()}{direction}{nulls}" for c in column.split(","))
        queries.append(f"SELECT id, a, b FROM r{where} ORDER BY {keys}, id{direction}{limit}")
    with Database(tmp_path / "fast.db", max_keys=4) as fast, \
            Database(tmp_path / "slow.db", max_keys=4, optimize=False) as slow:
        for db in (fast, slow):
            db.execute_script(setup)
            db.execute(f"INSERT INTO r VALUES {rows}")
            db.execute("ANALYZE")
        for sql in queries:
            assert fast.execute(sql).rows == slow.execute(sql).rows, sql
