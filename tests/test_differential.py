"""Differential tests: Arcanum must return the same rows as SQLite.

The same random data is loaded into both engines. A hand-written corpus
covers every feature, and a generator produces hundreds of random typed
queries. Results are compared as multisets unless the query's ORDER BY fixes
the row order completely.

Arcanum runs with tiny B+tree nodes (max_keys=4) so every query also
exercises multi-level trees.
"""

import random
import sqlite3
from collections import Counter

import pytest

from arcanum import Database

SCHEMA = """
CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept TEXT, salary REAL, manager_id INTEGER,
                  active BOOLEAN, hired INTEGER);
CREATE TABLE dept (name TEXT PRIMARY KEY, budget INTEGER, floor INTEGER);
CREATE TABLE sales (id INTEGER PRIMARY KEY, emp_id INTEGER, day INTEGER, amount INTEGER, region TEXT);
CREATE TABLE tags (emp_id INTEGER, tag TEXT);
"""

DEPTS = ["eng", "ops", "sales", "hr", "legal"]  # legal has nobody; some employees have no department
NAMES = ["Ada", "Bob", "Cy", "Di", "Ed", "Flo", "Gus", "Hal", "Ivy", "Jo", "Kim", "Lu", "Max", "Ned", "Oz"]


def _literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def _data():
    r = random.Random(2024)
    maybe = lambda value, p=0.15: None if r.random() < p else value  # noqa: E731
    rows = {"dept": [], "emp": [], "sales": [], "tags": []}
    for d in DEPTS:
        rows["dept"].append((d, maybe(r.randrange(50, 500) * 1000), maybe(r.randint(1, 9))))
    for i in range(1, 121):
        name = f"{r.choice(NAMES)}{'' if r.random() < 0.5 else ' ' + r.choice(NAMES).lower()}"
        rows["emp"].append((i, name, maybe(r.choice(DEPTS[:4])), maybe(float(r.randrange(40, 200)) + r.choice([0, 0.5])),
                            maybe(r.randint(1, i - 1) if i > 1 else None, 0.2), maybe(r.random() < 0.7),
                            r.randint(2010, 2024)))
    for i in range(1, 301):
        rows["sales"].append((i, maybe(r.randint(1, 130)), r.randint(1, 30), maybe(r.randint(-50, 900)),
                              maybe(r.choice(["north", "south", "east", "west"]))))
    for _ in range(150):
        rows["tags"].append((r.randint(1, 125), maybe(r.choice(["remote", "lead", "new", "oncall"]))))
    return rows


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    db = Database(tmp_path_factory.mktemp("diff") / "arcanum.db", max_keys=4)
    lite = sqlite3.connect(":memory:")
    lite.execute("PRAGMA case_sensitive_like = ON")
    db.execute_script(SCHEMA)
    lite.executescript(SCHEMA)
    for table, rows in _data().items():
        for start in range(0, len(rows), 50):
            sql = f"INSERT INTO {table} VALUES " + ", ".join(
                "(" + ", ".join(map(_literal, row)) + ")" for row in rows[start:start + 50])
            db.execute(sql)
            lite.execute(sql)
    for name in ("emp", "dept", "sales"):
        db.catalog.btree(db.catalog.tables[name].root).verify()
    yield db, lite
    db.close()


def _normalize(rows):
    def value(v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, float):
            v = round(v, 6)
            return int(v) if v.is_integer() else v
        return v
    return [tuple(value(v) for v in row) for row in rows]


def check(engines, sql, ordered=False):
    db, lite = engines
    expected = _normalize(lite.execute(sql).fetchall())
    actual = _normalize(db.execute(sql).rows)
    if ordered:
        assert actual == expected, sql
    else:
        assert Counter(actual) == Counter(expected), sql


CORPUS = [
    # filters, NULL logic and expressions
    ("SELECT * FROM emp WHERE id = 17", False),
    ("SELECT id, name FROM emp WHERE salary > 100 AND (dept = 'eng' OR dept IS NULL)", False),
    ("SELECT id FROM emp WHERE NOT (salary > 100) OR manager_id IN (1, 2, NULL)", False),
    ("SELECT id FROM emp WHERE manager_id NOT IN (1, 2, NULL)", False),
    ("SELECT id FROM emp WHERE salary BETWEEN 60 AND 90.5 AND hired NOT BETWEEN 2012 AND 2015", False),
    ("SELECT id, name FROM emp WHERE name LIKE 'A%' OR name LIKE '_o%' OR name NOT LIKE '%a%'", False),
    ("SELECT id, salary * 2 + 1, -salary, hired % 7, hired / 3, (hired - 2000) * -3 FROM emp", False),
    ("SELECT id, name || '/' || coalesce(dept, '?'), upper(name), lower(dept), length(name) FROM emp", False),
    ("SELECT id, substr(name, 2), substr(name, 1, 3), replace(name, 'a', 'A'), trim('  x ') FROM emp", False),
    ("SELECT id, abs(salary - 100), round(salary / 3, 2), round(salary) FROM emp WHERE salary IS NOT NULL", False),
    ("SELECT id, CASE WHEN salary > 150 THEN 'high' WHEN salary > 80 THEN 'mid' ELSE 'low' END FROM emp", False),
    ("SELECT id, CASE dept WHEN 'eng' THEN 1 WHEN 'ops' THEN 2 END, nullif(dept, 'hr') FROM emp", False),
    ("SELECT id, CAST(salary AS INTEGER), CAST(hired AS TEXT), CAST('42' AS INTEGER) + 1 FROM emp", False),
    ("SELECT id FROM emp WHERE active", False),
    ("SELECT id, active, active IS NULL, NOT active FROM emp WHERE active = FALSE OR active IS NULL", False),
    ("SELECT 1 + 2 * 3, 7 / 2, -7 / 2, 7 % -3, -7 % 3, 'a' || 'b', NULL + 1, 2.5 * 2", True),
    # sorting and limits
    ("SELECT id, name FROM emp ORDER BY id DESC LIMIT 5", True),
    ("SELECT id, salary FROM emp ORDER BY salary, id LIMIT 10 OFFSET 3", True),
    ("SELECT id, salary FROM emp ORDER BY salary DESC NULLS FIRST, id", True),
    ("SELECT id, salary FROM emp ORDER BY salary NULLS LAST, id DESC", True),
    ("SELECT id, dept FROM emp ORDER BY dept DESC, 1", True),
    ("SELECT name AS n, id FROM emp ORDER BY n, id", True),
    ("SELECT id FROM emp ORDER BY hired * -1, id", True),
    ("SELECT DISTINCT dept FROM emp", False),
    ("SELECT DISTINCT dept, active FROM emp ORDER BY dept, active", True),
    # aggregation
    ("SELECT count(*), count(salary), count(DISTINCT dept), sum(hired), avg(salary), min(name), max(salary) FROM emp", False),
    ("SELECT dept, count(*), sum(salary), avg(salary), min(hired), max(hired) FROM emp GROUP BY dept", False),
    ("SELECT dept, active, count(*) FROM emp GROUP BY dept, active", False),
    ("SELECT dept, count(*) AS c FROM emp GROUP BY dept HAVING count(*) > 20 ORDER BY c DESC, dept", True),
    ("SELECT hired / 5 AS bucket, count(*) FROM emp GROUP BY hired / 5 ORDER BY 1", True),
    ("SELECT dept, sum(salary) / count(*) FROM emp GROUP BY 1", False),
    ("SELECT count(*), sum(amount) FROM sales WHERE amount > 100000", False),
    ("SELECT region, day % 3, sum(amount), count(DISTINCT emp_id) FROM sales GROUP BY region, day % 3", False),
    ("SELECT dept FROM emp GROUP BY dept HAVING max(salary) > 190 OR min(salary) < 45", False),
    ("SELECT dept, count(*) FROM emp WHERE salary > 100 GROUP BY dept ORDER BY count(*) DESC, dept", True),
    # joins
    ("SELECT e.id, d.budget FROM emp e JOIN dept d ON e.dept = d.name", False),
    ("SELECT e.id, d.name, d.floor FROM emp e LEFT JOIN dept d ON e.dept = d.name AND d.floor > 3", False),
    ("SELECT e.id, d.name FROM emp e RIGHT JOIN dept d ON e.dept = d.name", False),
    ("SELECT e.id, d.name FROM emp e FULL OUTER JOIN dept d ON e.dept = d.name", False),
    ("SELECT e.name, m.name FROM emp e JOIN emp m ON e.manager_id = m.id WHERE m.salary < e.salary", False),
    ("SELECT e.id, s.id FROM emp e JOIN sales s ON s.emp_id = e.id AND s.amount > e.salary", False),
    ("SELECT e.id, s.id FROM emp e JOIN sales s ON s.emp_id < e.id / 20 WHERE e.id < 10", False),
    ("SELECT count(*) FROM emp e CROSS JOIN dept d", False),
    ("SELECT count(*) FROM emp, dept WHERE emp.dept = dept.name", False),
    ("SELECT * FROM emp JOIN dept ON emp.dept = dept.name WHERE emp.id < 20", False),
    ("SELECT emp.id, tags.tag FROM emp JOIN tags ON tags.emp_id = emp.id LEFT JOIN dept ON dept.name = emp.dept", False),
    ("SELECT * FROM tags JOIN emp USING (emp_id)".replace("emp USING (emp_id)", "sales USING (emp_id)"), False),
    ("SELECT d.name, count(e.id), coalesce(sum(e.salary), 0) FROM dept d LEFT JOIN emp e ON e.dept = d.name GROUP BY d.name", False),
    # subqueries
    ("SELECT id FROM emp WHERE dept IN (SELECT name FROM dept WHERE budget > 200000)", False),
    ("SELECT id FROM emp WHERE manager_id NOT IN (SELECT id FROM emp WHERE salary > 150)", False),
    ("SELECT id FROM emp WHERE id NOT IN (SELECT emp_id FROM sales)", False),
    ("SELECT id FROM emp e WHERE EXISTS (SELECT 1 FROM sales s WHERE s.emp_id = e.id AND s.amount > 800)", False),
    ("SELECT id FROM emp e WHERE NOT EXISTS (SELECT 1 FROM emp r WHERE r.manager_id = e.id)", False),
    ("SELECT id, (SELECT count(*) FROM sales s WHERE s.emp_id = e.id) FROM emp e", False),
    ("SELECT id FROM emp e WHERE salary > (SELECT avg(salary) FROM emp x WHERE x.dept = e.dept)", False),
    ("SELECT id, (SELECT max(amount) FROM sales) - hired FROM emp", False),
    ("SELECT sub.dept, sub.c FROM (SELECT dept, count(*) AS c FROM emp GROUP BY dept) AS sub WHERE sub.c > 10", False),
    ("SELECT id FROM emp e WHERE EXISTS (SELECT 1 FROM tags t WHERE t.emp_id = e.id AND t.tag IN "
     "(SELECT tag FROM tags WHERE emp_id = e.manager_id))", False),
    ("SELECT x.id FROM (SELECT id, salary FROM emp WHERE salary > 50) x JOIN (SELECT emp_id FROM sales) y ON y.emp_id = x.id", False),
    # set operations
    ("SELECT dept FROM emp UNION SELECT name FROM dept", False),
    ("SELECT dept FROM emp UNION ALL SELECT name FROM dept", False),
    ("SELECT dept FROM emp INTERSECT SELECT name FROM dept", False),
    ("SELECT name FROM dept EXCEPT SELECT dept FROM emp", False),
    ("SELECT id FROM emp WHERE id < 10 UNION SELECT emp_id FROM sales WHERE day = 3 ORDER BY 1", True),
    ("SELECT id, name FROM emp WHERE id < 5 UNION ALL SELECT id, region FROM sales WHERE id < 5 ORDER BY id, name", True),
    ("VALUES (1, 'a'), (2, NULL), (3, 'c')", False),
    # CTEs
    ("WITH big AS (SELECT * FROM emp WHERE salary > 150) SELECT dept, count(*) FROM big GROUP BY dept", False),
    ("WITH a AS (SELECT id FROM emp WHERE id < 30), b AS (SELECT id FROM a WHERE id > 10) SELECT * FROM b", False),
    ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50) SELECT sum(i), count(*) FROM n", False),
    ("WITH RECURSIVE chain(id, depth) AS (SELECT id, 0 FROM emp WHERE manager_id IS NULL "
     "UNION ALL SELECT e.id, c.depth + 1 FROM emp e JOIN chain c ON e.manager_id = c.id) "
     "SELECT depth, count(*) FROM chain GROUP BY depth", False),
    ("WITH RECURSIVE up(id, boss) AS (SELECT id, manager_id FROM emp WHERE id = 120 "
     "UNION SELECT e.id, e.manager_id FROM emp e JOIN up ON e.id = up.boss) SELECT * FROM up", False),
    ("WITH RECURSIVE fib(a, b, k) AS (SELECT 0, 1, 1 UNION ALL SELECT b, a + b, k + 1 FROM fib WHERE k < 40) "
     "SELECT a FROM fib ORDER BY k", True),
    # window functions
    ("SELECT id, row_number() OVER (ORDER BY hired, id) FROM emp", False),
    ("SELECT id, rank() OVER (PARTITION BY dept ORDER BY salary DESC), "
     "dense_rank() OVER (PARTITION BY dept ORDER BY salary DESC) FROM emp", False),
    ("SELECT id, sum(amount) OVER (PARTITION BY emp_id ORDER BY day, id) FROM sales", False),
    ("SELECT id, sum(amount) OVER (ORDER BY day) FROM sales", False),
    ("SELECT id, avg(amount) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND 1 FOLLOWING) FROM sales", False),
    ("SELECT id, count(*) OVER (), count(amount) OVER (PARTITION BY region), max(amount) OVER (PARTITION BY region) FROM sales", False),
    ("SELECT id, lag(amount) OVER (ORDER BY id), lead(amount, 2, -1) OVER (ORDER BY id) FROM sales", False),
    ("SELECT id, first_value(name) OVER (PARTITION BY dept ORDER BY id), "
     "last_value(name) OVER (PARTITION BY dept ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM emp", False),
    ("SELECT id, min(salary) OVER (ORDER BY id ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) FROM emp", False),
    ("SELECT id, sum(hired) OVER (ORDER BY dept RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) FROM emp", False),
    ("SELECT dept, sum(salary), rank() OVER (ORDER BY sum(salary) DESC) FROM emp GROUP BY dept", False),
    ("SELECT * FROM (SELECT id, dept, rank() OVER (PARTITION BY dept ORDER BY salary DESC, id) AS r FROM emp) x WHERE r <= 2", False),
    ("SELECT id, row_number() OVER (ORDER BY salary DESC, id) AS rn FROM emp ORDER BY rn LIMIT 5", True),
]


@pytest.mark.parametrize("sql,ordered", CORPUS, ids=[f"q{i}" for i in range(len(CORPUS))])
def test_corpus_matches_sqlite(engines, sql, ordered):
    check(engines, sql, ordered)


# ---- random queries -------------------------------------------------------------------

COLUMNS = {
    "emp": {"id": "I", "name": "T", "dept": "T", "salary": "R", "manager_id": "I", "hired": "I", "active": "B"},
    "sales": {"id": "I", "emp_id": "I", "day": "I", "amount": "I", "region": "T"},
}


class QueryGenerator:
    """Random well-typed SELECTs over emp and sales, avoiding the few places where
    SQLite and Arcanum differ on purpose (division by zero, mixed-type comparison)."""

    def __init__(self, seed):
        self.r = random.Random(seed)

    def column(self, scope, kind):
        options = [f"{alias}.{c}" for alias, table in scope for c, k in COLUMNS[table].items() if k == kind]
        return self.r.choice(options) if options else None

    def expr(self, scope, kind, depth=0):
        r = self.r
        leaf = depth >= 2 or r.random() < 0.4
        if kind in ("I", "R"):
            if leaf:
                col = self.column(scope, kind)
                if col and r.random() < 0.75:
                    return col
                return str(r.randint(-5, 60)) if kind == "I" else f"{r.randint(0, 99)}.5"
            choice = r.randrange(6)
            a, b = self.expr(scope, kind, depth + 1), self.expr(scope, "I", depth + 1)
            if choice == 0:
                return f"({a} {r.choice('+-*')} {b})"
            if choice == 1:
                return f"abs({a})"
            if choice == 2:
                return f"({a} {r.choice('/%') if kind == 'I' else '/'} {r.randint(1, 7)})"
            if choice == 3:
                return f"CASE WHEN {self.expr(scope, 'B', depth + 1)} THEN {a} ELSE {self.expr(scope, kind, depth + 1)} END"
            if choice == 4:
                return f"coalesce({a}, {b})"
            return f"-({a})"
        if kind == "T":
            if leaf:
                col = self.column(scope, "T")
                return col if col and r.random() < 0.8 else f"'{r.choice(['eng', 'ops', 'north', 'Ada', 'x'])}'"
            a = self.expr(scope, "T", depth + 1)
            return r.choice([f"upper({a})", f"lower({a})", f"substr({a}, 1, 2)", f"({a} || '-')",
                             f"coalesce({a}, 'none')"])
        # boolean
        if leaf:
            col = self.column(scope, "B")
            if col and r.random() < 0.2:
                return col
            t = r.choice("IIRT")
            op = r.choice(["=", "!=", "<", "<=", ">", ">="])
            return f"({self.expr(scope, t, depth + 1)} {op} {self.expr(scope, t, depth + 1)})"
        choice = r.randrange(7)
        if choice == 0:
            return f"({self.expr(scope, 'B', depth + 1)} {r.choice(['AND', 'OR'])} {self.expr(scope, 'B', depth + 1)})"
        if choice == 1:
            return f"NOT {self.expr(scope, 'B', depth + 1)}"
        if choice == 2:
            return f"({self.expr(scope, r.choice('IT'), depth + 1)} IS {r.choice(['', 'NOT '])}NULL)"
        if choice == 3:
            return f"({self.expr(scope, 'I', depth + 1)} {r.choice(['', 'NOT '])}BETWEEN {r.randint(-5, 20)} AND {r.randint(10, 80)})"
        if choice == 4:
            items = ", ".join(r.choice([str(r.randint(0, 40)), "NULL"]) for _ in range(r.randint(1, 4)))
            return f"({self.expr(scope, 'I', depth + 1)} {r.choice(['', 'NOT '])}IN ({items}))"
        if choice == 5:
            return f"({self.expr(scope, 'T', depth + 1)} LIKE '{r.choice(['%a%', 'A%', '_o%', '%', 'e_g'])}')"
        inner = "x" + str(depth)
        return (f"{r.choice(['', 'NOT '])}EXISTS (SELECT 1 FROM sales {inner} WHERE {inner}.emp_id = "
                f"{self.column(scope, 'I')} AND {inner}.amount > {r.randint(0, 800)})")

    def query(self):
        r = self.r
        scope = [("e", "emp")]
        from_ = "emp e"
        if r.random() < 0.4:
            scope.append(("s", "sales"))
            from_ += f" {r.choice(['JOIN', 'LEFT JOIN'])} sales s ON s.emp_id = e.id"
            if r.random() < 0.3:
                from_ += f" AND {self.expr(scope, 'B', 1)}"
        where = f" WHERE {self.expr(scope, 'B')}" if r.random() < 0.8 else ""
        if r.random() < 0.35:
            groups = [self.column(scope, r.choice("IT")) for _ in range(r.randint(1, 2))]
            aggs = [f"{r.choice(['count', 'sum', 'min', 'max', 'avg'])}({self.expr(scope, 'I', 1)})" for _ in range(2)]
            aggs.append("count(*)")
            having = f" HAVING count(*) > {r.randint(0, 3)}" if r.random() < 0.4 else ""
            return f"SELECT {', '.join(groups + aggs)} FROM {from_}{where} GROUP BY {', '.join(groups)}{having}"
        exprs = [self.expr(scope, r.choice("IRTB")) for _ in range(r.randint(1, 4))]
        distinct = "DISTINCT " if r.random() < 0.2 else ""
        return f"SELECT {distinct}{', '.join(exprs)} FROM {from_}{where}"


@pytest.mark.parametrize("seed", range(8))
def test_random_queries_match_sqlite(engines, seed):
    generator = QueryGenerator(seed)
    for _ in range(50):
        check(engines, generator.query())
