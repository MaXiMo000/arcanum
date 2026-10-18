"""The optimizer's recorded decisions: what a UI shows to explain why a plan looks the way it does."""

import pytest

from arcanum import Database


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=8) as database:
        database.execute_script("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
            CREATE INDEX orders_customer ON orders (customer_id);
            INSERT INTO customers SELECT n, 'c' || n, 'x' FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 50) SELECT n FROM s) AS q;
            INSERT INTO orders SELECT n, n % 50 + 1, n % 300, CASE WHEN n % 9 = 0 THEN 'open' ELSE 'done' END FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 600) SELECT n FROM s) AS q;
            ANALYZE;
        """)
        yield database


def decisions(db, sql, phase):
    result = db.execute(sql, trace=True)
    return result, [d for d in result.planner if d["phase"] == phase]


def plan_ids(plan):
    ids, stack = set(), [plan]
    while stack:
        node = stack.pop()
        ids.add(node["id"])
        stack.extend(node["children"])
        stack.extend(s["plan"] for s in node["subplans"])
    return ids


def test_access_paths_list_every_candidate_with_its_cost(db):
    result, [access] = decisions(db, "SELECT * FROM orders WHERE customer_id = 7", "access")
    paths = {c["path"]: c for c in access["candidates"]}
    seq, index = paths["Seq Scan on orders"], paths["Index Scan using orders_customer"]
    assert index["chosen"] and not seq["chosen"] and index["cost"] < seq["cost"] == 600
    assert paths["Index Scan using primary key"]["cost"] is None  # no condition on id
    assert "not usable" in paths["Index Scan using primary key"]["why"]
    assert access["node"] in plan_ids(result.plan)
    assert result.planning_ms > 0 and result.execution_ms > 0


def test_a_covering_index_is_costed_as_index_only(db):
    _, [access] = decisions(db, "SELECT id, customer_id FROM orders WHERE customer_id > 3", "access")
    chosen = next(c for c in access["candidates"] if c["chosen"])
    assert chosen["path"] == "Index Only Scan using orders_customer" and "no table lookups" in chosen["why"]
    _, [marked] = decisions(db, "SELECT id, customer_id FROM orders WHERE customer_id > 3", "index-only")
    assert "never read" in marked["detail"]


def test_join_order_and_pushdown_are_explained(db):
    sql = "SELECT c.name, o.amount FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.amount > 290"
    result, [join] = decisions(db, sql, "join")
    assert sum(c["chosen"] for c in join["candidates"]) == 1 and len(join["candidates"]) == 2
    assert all("Hash Join" in c["why"] for c in join["candidates"])
    assert join["node"] in plan_ids(result.plan)
    pushed = [d["title"] for d in result.planner if d["phase"] == "pushdown"]
    assert any("amount > 290" in t and "below the join" in t for t in pushed)


def test_subquery_rewrites_are_explained(db):
    sql = "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"
    _, [rewrite] = decisions(db, sql, "rewrite")
    assert "semi join" in rewrite["title"]
    sql = "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id > c.id)"
    _, [kept] = decisions(db, sql, "rewrite")
    assert "SubPlan" in kept["title"]


def test_every_node_has_a_cost_that_includes_its_inputs(db):
    plan = db.execute("SELECT c.city, count(*) FROM customers c JOIN orders o ON o.customer_id = c.id "
                      "GROUP BY c.city ORDER BY 2", trace=True).plan
    stack = [plan]
    while stack:
        node = stack.pop()
        assert node["est_cost"] is not None
        assert all(node["est_cost"] >= child["est_cost"] for child in node["children"])
        stack.extend(node["children"])
