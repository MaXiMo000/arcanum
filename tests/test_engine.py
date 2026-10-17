import pytest

from arcanum import ConstraintError, Database, ExecutionError, SqlError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, age INTEGER DEFAULT 30,
                                score REAL, admin BOOLEAN DEFAULT FALSE);
            INSERT INTO users (id, email, score) VALUES (1, 'a@x', 1), (2, 'b@x', 2.5), (3, 'c@x', NULL);
        """)
        yield database


def rows(db, sql):
    return db.execute(sql).rows


# ---- writes and constraints -------------------------------------------------------------


def test_defaults_and_coercion(db):
    assert rows(db, "SELECT id, age, score, admin FROM users ORDER BY id") == [
        (1, 30, 1.0, False), (2, 30, 2.5, False), (3, 30, None, False)]


def test_primary_key_unique_and_not_null(db):
    with pytest.raises(ConstraintError, match="primary key"):
        db.execute("INSERT INTO users (id, email) VALUES (1, 'new@x')")
    with pytest.raises(ConstraintError, match="unique index users_email_key"):
        db.execute("INSERT INTO users (id, email) VALUES (9, 'a@x')")
    with pytest.raises(ConstraintError, match="NOT NULL"):
        db.execute("INSERT INTO users (id, email) VALUES (9, NULL)")
    assert rows(db, "SELECT count(*) FROM users") == [(3,)]


def test_failed_statement_changes_nothing(db):
    # The second row collides, so the first must not stay behind.
    with pytest.raises(ConstraintError):
        db.execute("INSERT INTO users (id, email) VALUES (10, 'new@x'), (11, 'a@x')")
    assert rows(db, "SELECT id FROM users WHERE id >= 10") == []
    assert db.execute("INSERT INTO users (id, email) VALUES (10, 'new@x')").rowcount == 1


def test_update_can_shift_keys_and_swap_unique_values(db):
    assert db.execute("UPDATE users SET id = id + 1").rowcount == 3
    assert rows(db, "SELECT id FROM users ORDER BY id") == [(2,), (3,), (4,)]
    db.execute("UPDATE users SET email = CASE email WHEN 'a@x' THEN 'b@x' WHEN 'b@x' THEN 'a@x' ELSE email END")
    assert rows(db, "SELECT id, email FROM users ORDER BY id") == [(2, 'b@x'), (3, 'a@x'), (4, 'c@x')]
    with pytest.raises(ConstraintError):
        db.execute("UPDATE users SET email = 'same'")


def test_insert_select_from_same_table(db):
    db.execute("INSERT INTO users (id, email) SELECT id + 100, email || '.copy' FROM users")
    assert rows(db, "SELECT count(*) FROM users") == [(6,)]


def test_tables_without_primary_key_use_row_ids(db):
    db.execute("CREATE TABLE log (msg TEXT)")
    db.execute("INSERT INTO log VALUES ('one'), ('two'), ('one')")
    db.execute("UPDATE log SET msg = 'uno' WHERE msg = 'one'")
    assert db.execute("DELETE FROM log WHERE msg = 'two'").rowcount == 1
    db.execute("INSERT INTO log VALUES ('three')")
    assert rows(db, "SELECT msg FROM log") == [("uno",), ("uno",), ("three",)]  # row-id order survives UPDATE


def test_indexes_follow_updates_and_deletes(db):
    db.execute("CREATE INDEX users_age ON users (age)")
    db.execute("UPDATE users SET age = id * 10")
    db.execute("DELETE FROM users WHERE id = 2")
    index = db.catalog.indexes["users_age"]
    from arcanum.storage import decode
    entries = [decode(k) for k, _ in db.catalog.btree(index.root).scan()]
    assert entries == [(10, 1), (30, 3)]
    with pytest.raises(ConstraintError):
        db.execute("CREATE UNIQUE INDEX dup ON users (admin)")  # existing rows already collide


def test_drop_table_frees_its_pages(db):
    db.execute("CREATE TABLE big (n INTEGER PRIMARY KEY, pad TEXT)")
    db.execute("INSERT INTO big SELECT i, 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' FROM "
               "(WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 300) SELECT i FROM s) AS seq")
    pages = db.pager.page_count
    db.execute("DROP TABLE big")
    db.execute("CREATE TABLE big2 (n INTEGER PRIMARY KEY, pad TEXT)")
    db.execute("INSERT INTO big2 SELECT id, email FROM users")
    assert db.pager.page_count == pages  # new tree reuses the freed pages
    with pytest.raises(SqlError, match="does not exist"):
        db.execute("SELECT * FROM big")


# ---- transactions and persistence ---------------------------------------------------------


def test_transactions(db):
    db.execute("BEGIN")
    db.execute("DELETE FROM users")
    db.execute("CREATE TABLE temp (x INTEGER)")
    assert rows(db, "SELECT count(*) FROM users") == [(0,)]
    db.execute("ROLLBACK")
    assert rows(db, "SELECT count(*) FROM users") == [(3,)]
    assert "temp" not in db.catalog.tables

    db.execute("BEGIN")
    db.execute("INSERT INTO users (id, email) VALUES (4, 'd@x')")
    with pytest.raises(ConstraintError):
        db.execute("INSERT INTO users (id, email) VALUES (5, 'd@x')")  # fails alone; the transaction goes on
    db.execute("COMMIT")
    assert rows(db, "SELECT id FROM users ORDER BY id") == [(1,), (2,), (3,), (4,)]

    with pytest.raises(SqlError, match="no transaction"):
        db.execute("COMMIT")


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "p.db"
    with Database(path, max_keys=4) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT UNIQUE)")
        db.execute("INSERT INTO t SELECT i, 'row ' || i FROM "
                   "(WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 200) SELECT i FROM s) AS q")
        db.execute("BEGIN")
        db.execute("DELETE FROM t")  # never committed
    with Database(path, max_keys=4) as db:
        assert rows(db, "SELECT count(*), max(label) FROM t") == [(200, "row 99")]
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (500, 'row 7')")  # the unique index came back too


# ---- query features SQLite can't check ---------------------------------------------------------


def test_intersect_all_and_except_all(db):
    a = "VALUES (1), (1), (1), (2), (3)"
    b = "VALUES (1), (1), (3), (4)"
    assert sorted(rows(db, f"{a} INTERSECT ALL {b}")) == [(1,), (1,), (3,)]
    assert sorted(rows(db, f"{a} EXCEPT ALL {b}")) == [(1,), (2,)]


def test_limit_stops_an_endless_recursive_query(db):
    sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT i FROM n LIMIT 5"
    assert rows(db, sql) == [(1,), (2,), (3,), (4,), (5,)]


def test_runtime_errors(db):
    with pytest.raises(ExecutionError, match="division by zero"):
        db.execute("SELECT 1 / (id - 1) FROM users")
    with pytest.raises(ExecutionError, match="more than one row"):
        db.execute("SELECT (SELECT id FROM users)")
    with pytest.raises(ExecutionError, match="integer overflow"):
        db.execute("SELECT 9223372036854775807 + 1")
    with pytest.raises(ExecutionError, match="cannot convert"):
        db.execute("SELECT CAST('abc' AS INTEGER)")


def test_explain_and_explain_analyze(db):
    sql = ("EXPLAIN SELECT email FROM users u WHERE EXISTS "
           "(SELECT 1 FROM users v WHERE v.id = u.id + 1) ORDER BY email")
    plan = [line for (line,) in rows(db, sql)]
    assert plan[0].startswith("Sort")
    assert any("HashJoin SEMI on (id + 1) = id" in line for line in plan)  # decorrelated
    db.optimize = False
    assert any("SubPlan (exists, correlated)" in line for (line,) in rows(db, sql))
    db.optimize = True
    analyzed = rows(db, "EXPLAIN ANALYZE SELECT * FROM users WHERE id > 1")
    assert "IndexScan users using primary key (id > 1)" in analyzed[1][0]
    assert "rows=2" in analyzed[0][0] and "rows=2" in analyzed[1][0]


def test_events_show_the_engine_working(db):
    seen = []
    db.events.subscribe(lambda kind, data: seen.append(kind))
    db.execute("INSERT INTO users (id, email) SELECT id + 10, email || '!' FROM users")
    assert {"page_read", "page_write", "node_visit", "node_split"} <= set(seen)


# ---- errors point at the problem -----------------------------------------------------------


@pytest.mark.parametrize("sql,message", [
    ("SELECT nope FROM users", "line 1, col 8: column nope does not exist"),
    ("SELECT * FROM missing", "line 1, col 15: table missing does not exist"),
    ("SELECT x.id FROM users", "line 1, col 8: missing FROM entry for table x"),
    ("SELECT id FROM users a JOIN users b ON a.id = b.id", "column reference id is ambiguous"),
    ("SELECT email + 1 FROM users", "line 1, col 14: operator + expects INTEGER or REAL, not TEXT"),
    ("SELECT * FROM users WHERE id = 'one'", "line 1, col 30: cannot compare INTEGER with TEXT"),
    ("SELECT * FROM users WHERE age", "WHERE must be a boolean condition, not INTEGER"),
    ("SELECT age, count(*) FROM users", "column age must appear in GROUP BY"),
    ("SELECT * FROM users WHERE count(*) > 1", "aggregate functions are not allowed in WHERE"),
    ("SELECT sum(sum(age)) FROM users", "cannot be nested"),
    ("SELECT row_number() FROM users", "row_number requires an OVER clause"),
    ("SELECT lenght(email) FROM users", "line 1, col 8: function lenght does not exist"),
    ("SELECT substr(1, 2) FROM users", "substr(INTEGER, INTEGER) does not match"),
    ("SELECT 1 UNION SELECT 1, 2", "same number of columns"),
    ("INSERT INTO users (id, email) VALUES (5, 6)", "column email is TEXT but the value is INTEGER"),
    ("INSERT INTO users (id) VALUES (1, 2)", "1 target columns but the values have 2"),
    ("SELECT DISTINCT email FROM users ORDER BY age", "ORDER BY expressions must appear in the select list"),
    ("SELECT * FROM users LIMIT -1", "LIMIT must not be negative"),
    ("SELECT 2.5 % 2", "operator % expects INTEGER, not REAL"),
    ("CREATE TABLE users (x INTEGER)", "table users already exists"),
    ("WITH RECURSIVE r(n) AS (SELECT n FROM r UNION SELECT 1) SELECT * FROM r", "anchor part"),
])
def test_bind_errors(db, sql, message):
    with pytest.raises(SqlError, match=message.replace("(", r"\(").replace(")", r"\)").replace("*", r"\*").replace("+", r"\+")):
        db.execute(sql)
