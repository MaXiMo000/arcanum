"""Views, UPDATE ... FROM and DELETE ... USING."""

import pytest

from arcanum import Database, SqlError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "v.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
            CREATE INDEX orders_customer ON orders (customer_id);
            INSERT INTO customers VALUES (1, 'Ann', 'Oslo'), (2, 'Bo', 'Lima'), (3, 'Cy', 'Oslo');
            INSERT INTO orders VALUES (1, 1, 10, 'open'), (2, 1, 20, 'open'), (3, 2, 30, 'open'), (4, 3, 40, 'paid');
        """)
        yield database


def test_views_expand_where_they_are_used(db):
    db.execute("""CREATE VIEW totals (customer, total) AS
                  SELECT c.name, sum(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name""")
    assert db.execute("SELECT * FROM totals WHERE total > 25 ORDER BY customer").rows == [("Ann", 30.0), ("Bo", 30.0),
                                                                                         ("Cy", 40.0)]
    db.execute("CREATE VIEW big AS SELECT customer FROM totals WHERE total >= 40")  # a view on a view
    assert db.execute("SELECT * FROM big").rows == [("Cy",)]
    db.execute("INSERT INTO orders VALUES (5, 2, 100, 'open')")
    assert db.execute("SELECT count(*) FROM big").rows == [(2,)]  # views are not snapshots
    with pytest.raises(SqlError, match="because view totals depends on it"):
        db.execute("DROP TABLE orders")
    with pytest.raises(SqlError, match="because view big depends on it"):
        db.execute("DROP VIEW totals")
    with pytest.raises(SqlError, match="read-only"):
        db.execute("INSERT INTO big VALUES ('x')")
    db.execute("CREATE OR REPLACE VIEW big AS SELECT customer FROM totals WHERE total >= 100")
    assert db.execute("SELECT * FROM big").rows == [("Bo",)]
    db.execute("DROP VIEW big")
    db.execute("DROP VIEW totals")
    db.execute("DROP VIEW IF EXISTS totals")
    with pytest.raises(SqlError, match="column nope does not exist"):
        db.execute("CREATE VIEW bad AS SELECT nope FROM customers")


def test_views_survive_reopening(tmp_path):
    path = tmp_path / "r.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (a INTEGER)")
        db.execute("INSERT INTO t VALUES (1), (2)")
        db.execute("CREATE VIEW doubled AS SELECT a * 2 AS b FROM t")
    with Database(path) as db:
        assert db.execute("SELECT sum(b) FROM doubled").rows == [(6,)]


def test_update_from_and_delete_using(db):
    result = db.execute("""UPDATE orders SET status = 'vip-' || c.city
                           FROM customers c WHERE c.id = orders.customer_id AND c.city = 'Oslo' AND orders.amount > 15
                           RETURNING id, status""")
    assert sorted(result.rows) == [(2, "vip-Oslo"), (4, "vip-Oslo")]
    assert db.execute("DELETE FROM orders USING customers c WHERE c.id = orders.customer_id AND c.name = 'Bo'").status \
        == "DELETE 1"
    assert db.execute("SELECT id FROM orders ORDER BY id").rows == [(1,), (2,), (4,)]
    # Several matches: the row is updated once.
    db.execute("UPDATE customers SET name = name || '!' FROM orders o WHERE o.customer_id = customers.id")
    assert db.execute("SELECT name FROM customers ORDER BY id").rows == [("Ann!",), ("Bo",), ("Cy!",)]


def test_full_join_using_keeps_a_proper_type(db):
    rows = db.execute("SELECT customer_id, name FROM orders FULL JOIN (SELECT id AS customer_id, name FROM customers) c "
                      "USING (customer_id) ORDER BY customer_id, name").rows
    assert rows[0] == (1, "Ann")
