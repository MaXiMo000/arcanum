"""ALTER TABLE: columns, defaults, types, constraints and renames."""

import datetime
from decimal import Decimal

import pytest

from arcanum import ConstraintError, Database, SqlError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "a.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
            CREATE TABLE orders (id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES customers (id),
                                 amount INTEGER CHECK (amount > 0), note TEXT);
            CREATE INDEX orders_note ON orders (note);
            INSERT INTO customers VALUES (1, 'Ann', 'Oslo'), (2, 'Bo', NULL);
            INSERT INTO orders (customer_id, amount, note) VALUES (1, 10, 'a'), (2, 20, 'b'), (1, 30, NULL);
        """)
        yield database


def rows(db, sql):
    return db.execute(sql).rows


def test_add_and_drop_columns(db):
    db.execute("ALTER TABLE orders ADD COLUMN placed DATE DEFAULT '2024-01-01'")
    db.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS placed DATE")  # already there: nothing to do
    db.execute("ALTER TABLE orders ADD COLUMN serial_no SERIAL")
    assert rows(db, "SELECT id, placed, serial_no FROM orders ORDER BY id") == [
        (1, datetime.date(2024, 1, 1), 1), (2, datetime.date(2024, 1, 1), 2), (3, datetime.date(2024, 1, 1), 3)]
    assert rows(db, "INSERT INTO orders (customer_id, amount) VALUES (2, 5) RETURNING placed, serial_no") == [
        (datetime.date(2024, 1, 1), 4)]
    with pytest.raises(ConstraintError, match="contains null values"):
        db.execute("ALTER TABLE orders ADD COLUMN must TEXT NOT NULL")
    db.execute("ALTER TABLE orders DROP COLUMN note")  # its index goes with it
    assert "orders_note" not in db.catalog.indexes
    assert db.execute("SELECT * FROM orders WHERE id = 1").columns == ["id", "customer_id", "amount", "placed",
                                                                     "serial_no"]
    db.execute("ALTER TABLE orders DROP COLUMN amount")  # and so does its CHECK
    db.execute("INSERT INTO orders (customer_id) VALUES (1)")
    with pytest.raises(SqlError, match="primary key"):
        db.execute("ALTER TABLE orders DROP COLUMN id")
    with pytest.raises(SqlError, match="part of the primary key"):
        db.execute("ALTER TABLE customers DROP COLUMN id")


def test_renames_follow_constraints_and_indexes(db):
    db.execute("ALTER TABLE orders RENAME COLUMN amount TO total")
    with pytest.raises(ConstraintError, match=r'CHECK \("total" > 0\)'):
        db.execute("INSERT INTO orders (customer_id, total) VALUES (1, -1)")
    db.execute("ALTER TABLE customers RENAME COLUMN id TO customer_no")
    with pytest.raises(ConstraintError, match="is not present in table customers"):
        db.execute("INSERT INTO orders (customer_id, total) VALUES (99, 1)")
    db.execute("ALTER TABLE customers RENAME TO clients")
    with pytest.raises(ConstraintError, match="is not present in table clients"):
        db.execute("INSERT INTO orders (customer_id, total) VALUES (99, 1)")
    assert rows(db, "SELECT name FROM clients WHERE customer_no = 2") == [("Bo",)]
    assert rows(db, "INSERT INTO orders (customer_id, total) VALUES (2, 1) RETURNING id") == [(4,)]


def test_defaults_not_null_and_types(db):
    db.execute("ALTER TABLE customers ALTER COLUMN city SET DEFAULT 'Lima'")
    db.execute("INSERT INTO customers (id, name) VALUES (3, 'Cy')")
    db.execute("ALTER TABLE customers ALTER COLUMN city DROP DEFAULT")
    db.execute("INSERT INTO customers (id, name) VALUES (4, 'Di')")
    assert rows(db, "SELECT city FROM customers WHERE id >= 3 ORDER BY id") == [("Lima",), (None,)]
    with pytest.raises(ConstraintError, match="contains null values"):
        db.execute("ALTER TABLE customers ALTER COLUMN city SET NOT NULL")
    db.execute("UPDATE customers SET city = '?' WHERE city IS NULL")
    db.execute("ALTER TABLE customers ALTER COLUMN city SET NOT NULL")
    with pytest.raises(ConstraintError, match="NOT NULL"):
        db.execute("INSERT INTO customers (id, name) VALUES (5, 'Ed')")
    db.execute("ALTER TABLE orders ALTER COLUMN amount TYPE NUMERIC(8, 2)")
    assert rows(db, "SELECT amount FROM orders WHERE id = 1") == [(Decimal("10.00"),)]
    db.execute("ALTER TABLE orders ALTER COLUMN note TYPE TEXT")
    with pytest.raises(Exception, match="cannot convert"):
        db.execute("ALTER TABLE orders ALTER COLUMN note TYPE INTEGER")


def test_constraints_can_be_added_and_dropped(db):
    with pytest.raises(ConstraintError, match="check constraint big"):
        db.execute("ALTER TABLE orders ADD CONSTRAINT big CHECK (amount > 15)")  # existing rows must pass
    db.execute("ALTER TABLE orders ADD CONSTRAINT positive CHECK (amount < 1000)")
    db.execute("ALTER TABLE orders DROP CONSTRAINT orders_amount_check")
    db.execute("INSERT INTO orders (customer_id, amount) VALUES (1, -5)")
    db.execute("ALTER TABLE customers ADD UNIQUE (name)")
    with pytest.raises(ConstraintError, match="duplicate"):
        db.execute("INSERT INTO customers VALUES (9, 'Ann', 'x')")
    db.execute("ALTER TABLE orders DROP CONSTRAINT orders_customer_id_fkey")
    db.execute("INSERT INTO orders (customer_id, amount) VALUES (77, 1)")
    with pytest.raises(ConstraintError, match="is not present"):
        db.execute("ALTER TABLE orders ADD FOREIGN KEY (customer_id) REFERENCES customers")
    db.execute("DELETE FROM orders WHERE customer_id = 77")
    db.execute("ALTER TABLE orders ADD FOREIGN KEY (customer_id) REFERENCES customers ON DELETE CASCADE")
    db.execute("DELETE FROM customers WHERE id = 1")
    assert rows(db, "SELECT count(*) FROM orders") == [(1,)]
    with pytest.raises(SqlError, match="does not exist"):
        db.execute("ALTER TABLE orders DROP CONSTRAINT nope")
    db.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS nope")


def test_alter_survives_reopen_and_respects_views(tmp_path):
    path = tmp_path / "r.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'x')")
        db.execute("CREATE VIEW v AS SELECT a, b FROM t")
        with pytest.raises(SqlError, match="view v depends on it"):
            db.execute("ALTER TABLE t DROP COLUMN b")
        with pytest.raises(SqlError, match="view v depends on it"):
            db.execute("ALTER TABLE t RENAME TO u")
        db.execute("ALTER TABLE t ADD COLUMN c NUMERIC(6, 1) DEFAULT 2.5 CHECK (c > 0)")
    with Database(path) as db:
        assert db.execute("SELECT * FROM t").rows == [(1, "x", Decimal("2.5"))]
        assert db.execute("SELECT * FROM v").rows == [(1, "x")]
        with pytest.raises(ConstraintError, match="t_c_check"):
            db.execute("INSERT INTO t VALUES (2, 'y', -1)")
