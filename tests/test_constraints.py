"""PostgreSQL features: identity columns, CHECK, foreign keys, RETURNING and INSERT ... ON CONFLICT."""

import pytest

from arcanum import ConstraintError, Database, LockWaitError, SqlError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE customers (id SERIAL PRIMARY KEY, email TEXT UNIQUE, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                customer_id INTEGER REFERENCES customers ON DELETE CASCADE,
                amount REAL CHECK (amount > 0),
                status TEXT DEFAULT 'new',
                CONSTRAINT known_status CHECK (status IN ('new', 'paid'))
            );
            CREATE TABLE notes (id SERIAL PRIMARY KEY, customer_id INTEGER, body TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE SET NULL);
        """)
        yield database


def rows(db, sql):
    return db.execute(sql).rows


def test_serial_and_identity_columns_number_rows_and_return_them(db):
    result = db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann'), ('b@x', 'Bo') RETURNING id, name")
    assert result.columns == ["id", "name"] and result.rows == [(1, "Ann"), (2, "Bo")]
    assert result.status == "INSERT 2"
    db.execute("INSERT INTO customers VALUES (10, 'c@x', 'Cy')")  # BY DEFAULT: an explicit value is allowed
    assert rows(db, "INSERT INTO customers (email, name) VALUES ('d@x', 'Di') RETURNING id") == [(3,)]
    with pytest.raises(ConstraintError, match="GENERATED ALWAYS"):
        db.execute("INSERT INTO orders (id, customer_id, amount) VALUES (5, 1, 9)")
    assert rows(db, "INSERT INTO orders (customer_id, amount) VALUES (1, 9.5) RETURNING *") == [(1, 1, 9.5, "new")]


def test_sequences_survive_reopening(tmp_path):
    path = tmp_path / "s.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (id SERIAL PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
    with Database(path) as db:
        assert rows(db, "INSERT INTO t (v) VALUES ('c') RETURNING id") == [(3,)]


def test_check_constraints(db):
    db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann')")
    with pytest.raises(ConstraintError, match=r"orders_amount_check: CHECK \(amount > 0\)"):
        db.execute("INSERT INTO orders (customer_id, amount) VALUES (1, -5)")
    with pytest.raises(ConstraintError, match="known_status"):
        db.execute("INSERT INTO orders (customer_id, amount, status) VALUES (1, 5, 'lost')")
    db.execute("INSERT INTO orders (customer_id, amount) VALUES (1, NULL)")  # NULL passes a CHECK
    with pytest.raises(ConstraintError, match="orders_amount_check"):
        db.execute("UPDATE orders SET amount = 0")
    with pytest.raises(SqlError, match="column nope does not exist"):
        db.execute("CREATE TABLE bad (a INTEGER CHECK (nope > 1))")
    assert "bad" not in db.catalog.tables


def test_foreign_keys_refuse_orphans_and_cascade(db):
    db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann'), ('b@x', 'Bo')")
    with pytest.raises(ConstraintError, match=r"key \(customer_id\)=\(9\) is not present in table customers"):
        db.execute("INSERT INTO orders (customer_id, amount) VALUES (9, 1)")
    db.execute("INSERT INTO orders (customer_id, amount) VALUES (1, 1), (1, 2), (2, 3), (NULL, 4)")
    db.execute("INSERT INTO notes (customer_id, body) VALUES (1, 'vip'), (2, 'new')")
    with pytest.raises(ConstraintError, match="is not present"):
        db.execute("UPDATE orders SET customer_id = 7 WHERE customer_id = 2")
    events = []
    db.events.subscribe(lambda kind, data: events.append((kind, data)))
    assert db.execute("DELETE FROM customers WHERE id = 1").status == "DELETE 1"
    assert rows(db, "SELECT customer_id, amount FROM orders ORDER BY amount") == [(2, 3.0), (None, 4.0)]
    assert rows(db, "SELECT customer_id, body FROM notes ORDER BY id") == [(None, "vip"), (2, "new")]
    cascades = {d["constraint"]: (d["action"], d["rows"]) for k, d in events if k == "cascade"}
    assert cascades == {"orders_customer_id_fkey": ("CASCADE", 2), "notes_customer_id_fkey": ("SET NULL", 1)}
    with pytest.raises(SqlError, match="cannot drop table customers"):
        db.execute("DROP TABLE customers")


def test_restrict_and_key_updates(tmp_path):
    with Database(tmp_path / "r.db") as db:
        db.execute_script("""
            CREATE TABLE p (id INTEGER PRIMARY KEY, code TEXT UNIQUE);
            CREATE TABLE c (id INTEGER PRIMARY KEY, code TEXT REFERENCES p (code) ON DELETE RESTRICT);
            INSERT INTO p VALUES (1, 'x'), (2, 'y');
            INSERT INTO c VALUES (1, 'x');
        """)
        with pytest.raises(ConstraintError, match=r"still referenced from table c"):
            db.execute("DELETE FROM p WHERE id = 1")
        with pytest.raises(ConstraintError, match="still referenced"):
            db.execute("UPDATE p SET code = 'z' WHERE id = 1")
        db.execute("UPDATE p SET code = 'w' WHERE id = 2")  # nobody references y
        with pytest.raises(SqlError, match="no unique constraint matching"):
            db.execute("CREATE TABLE d (n INTEGER REFERENCES c (id), m TEXT REFERENCES c (code))")


def test_self_referencing_rows_in_one_statement(tmp_path):
    with Database(tmp_path / "e.db") as db:
        db.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, boss INTEGER REFERENCES emp ON DELETE CASCADE)")
        db.execute("INSERT INTO emp VALUES (2, 1), (1, NULL), (3, 2)")  # checked at the end of the statement
        db.execute("DELETE FROM emp WHERE id = 1")
        assert rows(db, "SELECT count(*) FROM emp") == [(0,)]


def test_on_conflict(db):
    db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann')")
    nothing = db.execute("INSERT INTO customers (id, email, name) VALUES (1, 'z@x', 'Zed') ON CONFLICT DO NOTHING")
    assert nothing.status == "INSERT 0"
    upsert = db.execute("""INSERT INTO customers (email, name) VALUES ('a@x', 'Annie'), ('b@x', 'Bo')
                           ON CONFLICT (email) DO UPDATE SET name = excluded.name || ' (' || customers.name || ')'
                           RETURNING id, email, name""")
    assert upsert.rows == [(1, "a@x", "Annie (Ann)"), (3, "b@x", "Bo")]
    skipped = db.execute("INSERT INTO customers (email, name) VALUES ('b@x', 'B') "
                         "ON CONFLICT (email) DO UPDATE SET name = excluded.name WHERE customers.name = 'nobody'")
    assert skipped.status == "INSERT 0" and rows(db, "SELECT name FROM customers WHERE email = 'b@x'") == [("Bo",)]
    with pytest.raises(ConstraintError, match="cannot affect row a second time"):
        db.execute("INSERT INTO customers (email, name) VALUES ('c@x', '1'), ('c@x', '2') "
                   "ON CONFLICT (email) DO UPDATE SET name = excluded.name")
    with pytest.raises(SqlError, match="no unique or exclusion constraint"):
        db.execute("INSERT INTO customers (email, name) VALUES ('q', 'q') ON CONFLICT (name) DO NOTHING")


def test_returning_from_update_and_delete(db):
    db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann'), ('b@x', 'Bo')")
    assert rows(db, "UPDATE customers SET name = upper(name) WHERE id = 2 RETURNING id, name AS shout") == [(2, "BO")]
    deleted = db.execute("DELETE FROM customers WHERE id < 3 RETURNING *")
    assert deleted.columns == ["id", "email", "name"] and len(deleted.rows) == 2


def test_foreign_keys_wait_for_open_transactions(db):
    db.execute("INSERT INTO customers (email, name) VALUES ('a@x', 'Ann')")
    a, b = db.connect("a"), db.connect("b")
    a.execute("BEGIN")
    a.execute("DELETE FROM customers WHERE id = 1")
    with pytest.raises(LockWaitError):  # PostgreSQL would wait to see whether the delete commits
        b.execute("INSERT INTO orders (customer_id, amount) VALUES (1, 5)")
    a.execute("ROLLBACK")
    b.execute("INSERT INTO orders (customer_id, amount) VALUES (1, 5)")
