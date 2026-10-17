"""The optimizer's choices: what plans it picks, and that they do less work.

Correctness of every rewrite against SQLite is covered by test_differential.py;
these tests check the shapes and costs themselves.
"""

import pytest

from arcanum import Database, QueryLimits


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=8) as database:
        database.execute_script("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer INTEGER, status TEXT, total REAL);
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT);
            CREATE INDEX orders_customer ON orders (customer);
            CREATE INDEX orders_status ON orders (status);
            INSERT INTO customers SELECT n, 'c' || n, CASE WHEN n % 10 = 0 THEN 'north' ELSE 'south' END FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 100) SELECT n FROM s) AS q;
            INSERT INTO orders SELECT n, n % 100 + 1, CASE WHEN n % 50 = 0 THEN 'open' ELSE 'closed' END,
                                      CASE WHEN n % 7 = 0 THEN NULL ELSE n % 300 END FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 2000) SELECT n FROM s) AS q;
            ANALYZE;
        """)
        yield database


def plan(db, sql):
    return "\n".join(line for (line,) in db.execute("EXPLAIN " + sql).rows)


def pages_read(db, sql):
    reads = []
    unsubscribe = db.events.subscribe(lambda kind, data: reads.append(1) if kind == "page_read" else None)
    rows = db.execute(sql).rows
    unsubscribe()
    return len(reads), rows


# ---- access paths ---------------------------------------------------------------------------


def test_point_lookup_reads_a_root_to_leaf_path(db):
    sql = "SELECT * FROM orders WHERE id = 1234"
    assert "IndexScan orders using primary key (id = 1234)" in plan(db, sql)
    indexed, rows = pages_read(db, sql)
    db.optimize = False
    scanned, same = pages_read(db, sql)
    assert rows == same == [(1234, 35, "closed", 34.0)]
    assert indexed <= 6 and scanned > 50 * indexed


def test_cost_model_picks_index_only_when_selective(db):
    assert "IndexScan orders using orders_status (status = 'open')" in plan(db, "SELECT id FROM orders WHERE status = 'open'")
    # 98% of orders are closed: walking the index and fetching each row would cost more than a scan.
    assert "TableScan orders" in plan(db, "SELECT id FROM orders WHERE status = 'closed'")
    assert "IndexScan orders using primary key (id >= 1990)" in plan(db, "SELECT id FROM orders WHERE id >= 1990")
    assert "TableScan orders" in plan(db, "SELECT id FROM orders WHERE id >= 10")


def test_index_ranges_handle_nulls_types_and_composite_keys(db):
    db.execute_script("""
        CREATE TABLE m (a INTEGER, b REAL, c TEXT, PRIMARY KEY (a, b));
        INSERT INTO m VALUES (1, 1.5, 'x'), (1, 2.0, 'y'), (1, 3.5, NULL), (2, 1.0, 'z'), (2, 2.5, NULL);
        CREATE INDEX m_c ON m (c);
    """)
    assert "primary key (a = 1 AND b >= 2)" in plan(db, "SELECT c FROM m WHERE a = 1 AND b >= 2")
    assert db.execute("SELECT c FROM m WHERE a = 1 AND b >= 2").rows == [("y",), (None,)]  # integer bound on REAL
    assert db.execute("SELECT a, b FROM m WHERE a = 1 AND b < 3").rows == [(1, 1.5), (1, 2.0)]
    assert sorted(db.execute("SELECT c FROM m WHERE c < 'z'").rows) == [("x",), ("y",)]  # NULLs are not < 'z'
    assert db.execute("SELECT c FROM m WHERE c = NULL").rows == []
    assert db.execute("SELECT count(*) FROM m WHERE a > 1").rows == [(2,)]


def test_scalar_subquery_joins_or_looks_up_by_cost(db):
    sql = "SELECT name, (SELECT max(total) FROM orders o WHERE o.customer = c.id) FROM customers c"
    # 100 customers: grouping all orders once beats 100 index lookups.
    assert "HashJoin LEFT on id = customer" in plan(db, sql)
    # Two customers: two index lookups beat reading every order.
    assert "IndexScan orders using orders_customer (customer = outer.id)" in plan(db, sql + " WHERE c.id < 3")
    joined = db.execute(sql).rows
    db.optimize = False
    assert db.execute(sql).rows == joined


def test_scalar_count_is_zero_not_null_without_matches(db):
    db.execute("DELETE FROM orders WHERE customer IN (3, 4)")
    sql = ("SELECT id, (SELECT count(*) FROM orders o WHERE o.customer = c.id), "
           "(SELECT count(*) * 2 + 1 FROM orders o WHERE o.customer = c.id), "
           "(SELECT sum(total) FROM orders o WHERE o.customer = c.id) FROM customers c WHERE id BETWEEN 2 AND 4")
    assert db.execute(sql).rows == [(2, 20, 41, db.execute(
        "SELECT sum(total) FROM orders WHERE customer = 2").rows[0][0]), (3, 0, 1, None), (4, 0, 1, None)]
    assert db.execute("SELECT id FROM customers c WHERE (SELECT count(*) FROM orders o WHERE o.customer = c.id) = 0"
                      ).rows == [(3,), (4,)]


def test_in_lists_become_index_point_lookups(db):
    sql = "SELECT id, customer FROM orders WHERE id IN (1500, 7, NULL, 7, 99999)"
    assert "IndexScan orders using primary key (id IN (1500, 7, NULL, 7, 99999))" in plan(db, sql)
    reads, rows = pages_read(db, sql)
    db.optimize = False
    full_scan, same = pages_read(db, sql)
    db.optimize = True
    assert rows == same == [(7, 8), (1500, 1)] and reads * 5 < full_scan
    assert "IndexScan orders using orders_customer (customer IN (4, 5))" in plan(
        db, "SELECT id FROM orders WHERE customer IN (4, 5)")
    assert len(db.execute("SELECT id FROM orders WHERE customer IN (4, 5)").rows) == 40


def test_large_sorts_spill_to_disk_and_merge(tmp_path):
    spills = []
    with Database(tmp_path / "sort.db", limits=QueryLimits(sort_memory_rows=100)) as db:
        db.events.subscribe(lambda kind, data: spills.append(data) if kind == "sort_spill" else None)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER, s TEXT, flag BOOLEAN)")
        db.execute("INSERT INTO t SELECT n, (n * 7919) % 101, CASE WHEN n % 13 = 0 THEN NULL ELSE 'v' || (n % 37) END, "
                   "n % 2 = 0 FROM (WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 1000) "
                   "SELECT n FROM r) AS q")
        rows = db.execute("SELECT k, s, id, flag FROM t ORDER BY k DESC, s NULLS FIRST, id").rows
        assert spills and spills[-1] == {"runs": 10, "rows": 1000}  # ten sorted runs on disk, merged back
        expected = sorted(rows, key=lambda r: (-r[0], r[1] is not None, r[1] or "", r[2]))
        assert rows == expected and {type(r[3]) for r in rows} == {bool}


def test_update_and_delete_find_rows_through_indexes(db):
    reads, _ = pages_read(db, "UPDATE orders SET total = 0 WHERE id = 77")
    db.optimize = False
    full_scan, _ = pages_read(db, "UPDATE orders SET total = 0 WHERE id = 78")
    db.optimize = True
    assert reads * 5 < full_scan  # the row and its two index entries, not every page
    assert db.execute("SELECT total FROM orders WHERE id = 77").rows == [(0.0,)]
    assert db.execute("DELETE FROM orders WHERE customer = 5").rowcount == 20
    assert db.execute("SELECT count(*) FROM orders WHERE customer = 5").rows == [(0,)]
    assert db.execute("SELECT count(*) FROM orders").rows == [(1980,)]


# ---- rewrites --------------------------------------------------------------------------------


@pytest.mark.parametrize("sql,shape", [
    ("SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer = c.id AND o.status = 'open')",
     "HashJoin SEMI on id = customer"),
    ("SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer = c.id AND o.total > 290)",
     "HashJoin ANTI on id = customer"),
    ("SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer = c.id AND o.total > c.id * 3)",
     "HashJoin ANTI on id = customer filter (total > (id * 3))"),
    ("SELECT name FROM customers c WHERE c.id IN (SELECT customer FROM orders WHERE status = 'open')",
     "HashJoin SEMI on id = customer"),
])
def test_subqueries_become_semi_and_anti_joins(db, sql, shape):
    assert shape in plan(db, sql)
    optimized = sorted(db.execute(sql).rows)
    db.optimize = False
    assert "SubPlan" in plan(db, sql)
    assert sorted(db.execute(sql).rows) == optimized


def test_filters_move_below_aggregates_and_into_derived_tables(db):
    text = plan(db, "SELECT * FROM (SELECT customer, sum(total) AS s FROM orders GROUP BY customer) t WHERE t.customer = 9")
    lines = text.splitlines()
    aggregate = next(i for i, line in enumerate(lines) if "HashAggregate" in line)
    assert "IndexScan orders using orders_customer (customer = 9)" in "\n".join(lines[aggregate:])
    assert not any(line.strip().startswith("Filter") for line in lines)


def test_comma_joins_become_hash_joins_built_on_the_smaller_side(db):
    text = plan(db, "SELECT o.id FROM orders o, customers c WHERE o.customer = c.id AND c.region = 'north'")
    assert "NestedLoopJoin" not in text
    join = next(line for line in text.splitlines() if "HashJoin" in line)
    lines = text.splitlines()
    build = lines[lines.index(join) + 1:]
    # The right (build) input is the filtered customers, ten times smaller than orders.
    assert any("customers" in line for line in build[-2:])
    assert db.execute("SELECT count(*) FROM orders o, customers c WHERE o.customer = c.id AND c.region = 'north'").rows \
        == [(200,)]


def test_join_order_starts_from_the_most_selective_table(db):
    db.execute("CREATE TABLE regions (name TEXT PRIMARY KEY, manager TEXT)")
    db.execute("INSERT INTO regions VALUES ('north', 'Ada'), ('south', 'Bob')")
    db.execute("ANALYZE regions")
    sql = ("SELECT o.id FROM orders o JOIN customers c ON o.customer = c.id JOIN regions r ON r.name = c.region "
           "WHERE r.manager = 'Ada' AND o.status = 'open'")
    lines = plan(db, sql).splitlines()
    deepest_join = max(i for i, line in enumerate(lines) if "Join" in line)
    assert any("TableScan regions" in line for line in lines[deepest_join:])  # filtered to one row: joined first
    optimized = sorted(db.execute(sql.replace("'open'", "'closed'")).rows)
    db.optimize = False
    assert sorted(db.execute(sql.replace("'open'", "'closed'")).rows) == optimized and len(optimized) == 200


def test_statistics_survive_reopen_and_feed_estimates(tmp_path):
    path = tmp_path / "s.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER)")
        db.execute("INSERT INTO t SELECT n, n % 4 FROM (WITH RECURSIVE s(n) AS "
                   "(SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 400) SELECT n FROM s) AS q")
        db.execute("ANALYZE t")
    with Database(path) as db:
        stats = db.catalog.tables["t"].stats
        assert stats.rows == 400 and stats.columns[1].distinct == 4
        assert "est=100" in plan(db, "SELECT id FROM t WHERE k = 2")  # 400 rows / 4 distinct values
        db.execute("DROP TABLE t")
        assert not [k for k, _ in db.catalog.tree.scan()]  # the stats entries went with the table
