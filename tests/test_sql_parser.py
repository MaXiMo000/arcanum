import re

import pytest

from arcanum.sql import SqlError, parse, parse_script
from arcanum.sql import nodes as n
from arcanum.sql.lexer import FLOAT, INT, OP, QUOTED, STRING, WORD, tokenize


def col(name, table=None):
    return n.ColumnRef(name, table)


def lit(value):
    return n.Literal(value)


def body(sql):
    return parse(sql).body


# ---- lexer ---------------------------------------------------------------------


def test_tokens():
    toks = tokenize("SELECT \"Odd Name\", 'it''s', 1.5e3, 42 FROM t -- comment\n/* block */ WHERE a <> b")
    assert [(t.kind, t.value) for t in toks[:-1]] == [
        (WORD, "SELECT"), (QUOTED, "Odd Name"), (OP, ","), (STRING, "it's"), (OP, ","),
        (FLOAT, "1.5e3"), (OP, ","), (INT, "42"), (WORD, "FROM"), (WORD, "t"),
        (WORD, "WHERE"), (WORD, "a"), (OP, "<>"), (WORD, "b"),
    ]


@pytest.mark.parametrize("sql,message", [
    ("SELECT 'abc", "unterminated string"),
    ("SELECT 12abc", "invalid number"),
    ("SELECT 1 /* never closed", "unterminated comment"),
    ("SELECT 1 # 2", "unexpected character"),
])
def test_lexer_errors(sql, message):
    with pytest.raises(SqlError, match=message):
        tokenize(sql)


# ---- expressions -------------------------------------------------------------------


def test_precedence():
    expr = body("SELECT 1 + 2 * 3 = 7 AND NOT a OR b").columns[0].expr
    assert expr == n.Binary(
        "OR",
        n.Binary("AND",
                 n.Binary("=", n.Binary("+", lit(1), n.Binary("*", lit(2), lit(3))), lit(7)),
                 n.Unary("NOT", col("a"))),
        col("b"),
    )


def test_predicates():
    where = body("SELECT * FROM t WHERE a NOT IN (1, 2) AND b NOT BETWEEN 1 AND 5 "
                 "AND c IS NOT NULL AND d NOT LIKE 'x%' AND e <> -3").where
    parts = []
    while isinstance(where, n.Binary) and where.op == "AND":
        parts.append(where.right)
        where = where.left
    parts.append(where)
    assert parts[::-1] == [
        n.InList(col("a"), [lit(1), lit(2)], negated=True),
        n.Between(col("b"), lit(1), lit(5), negated=True),
        n.IsNull(col("c"), negated=True),
        n.Like(col("d"), lit("x%"), negated=True),
        n.Binary("!=", col("e"), lit(-3)),
    ]


def test_most_negative_integer_is_a_literal():
    assert body("SELECT -9223372036854775808").columns[0].expr == lit(-9223372036854775808)


def test_case_cast_and_functions():
    items = body("SELECT CASE WHEN x > 0 THEN 'pos' ELSE 'neg' END AS sign, CAST(y AS varchar(10)), "
                 "count(*), COUNT(DISTINCT z), coalesce(a, 0) FROM t").columns
    assert items[0] == n.SelectItem(n.Case(None, [(n.Binary(">", col("x"), lit(0)), lit("pos"))], lit("neg")), "sign")
    assert items[1].expr == n.Cast(col("y"), "TEXT")
    assert items[2].expr == n.Call("COUNT", star=True)
    assert items[3].expr == n.Call("COUNT", [col("z")], distinct=True)
    assert items[4].expr == n.Call("COALESCE", [col("a"), lit(0)])


def test_window_function():
    expr = body("SELECT sum(amount) OVER (PARTITION BY dept ORDER BY day DESC NULLS LAST "
                "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM sales").columns[0].expr
    assert expr == n.Call("SUM", [col("amount")], over=n.WindowSpec(
        partition_by=[col("dept")],
        order_by=[n.OrderItem(col("day"), desc=True, nulls_first=False)],
        frame=n.Frame("ROWS", n.FrameBound("PRECEDING", lit(2)), n.FrameBound("CURRENT ROW")),
    ))


# ---- queries -------------------------------------------------------------------


def test_simple_select():
    assert parse("SELECT id, name AS n FROM users u WHERE id = 42") == n.Query(n.Select(
        columns=[n.SelectItem(col("id")), n.SelectItem(col("name"), "n")],
        from_=n.TableRef("users", "u"),
        where=n.Binary("=", col("id"), lit(42)),
    ))


def test_identifiers_fold_case_unless_quoted():
    select = body('SELECT Name, "Name", T.* FROM Users T')
    assert select.columns == [n.SelectItem(col("name")), n.SelectItem(col("Name")), n.SelectItem(n.Star("t"))]


def test_contextual_keywords_are_valid_names():
    select = body("SELECT first, last, rows, key, index FROM range")
    assert [item.expr.name for item in select.columns] == ["first", "last", "rows", "key", "index"]


def test_joins():
    from_ = body("SELECT * FROM a JOIN b ON a.id = b.a_id LEFT OUTER JOIN c USING (id), d CROSS JOIN e").from_
    # Explicit JOINs bind tighter than the comma: (a JOIN b LEFT JOIN c), (d CROSS JOIN e)
    assert from_ == n.Join(
        "CROSS",
        n.Join("LEFT",
               n.Join("INNER", n.TableRef("a"), n.TableRef("b"),
                      on=n.Binary("=", col("id", "a"), col("a_id", "b"))),
               n.TableRef("c"), using=["id"]),
        n.Join("CROSS", n.TableRef("d"), n.TableRef("e")),
    )


def test_group_order_limit():
    q = parse("SELECT dept, avg(salary) FROM emp GROUP BY dept HAVING count(*) > 2 "
              "ORDER BY 2 DESC, dept LIMIT 10 OFFSET 5")
    assert q.body.group_by == [col("dept")]
    assert q.body.having == n.Binary(">", n.Call("COUNT", star=True), lit(2))
    assert q.order_by == [n.OrderItem(lit(2), desc=True), n.OrderItem(col("dept"))]
    assert (q.limit, q.offset) == (lit(10), lit(5))


def test_set_operations_precedence():
    # INTERSECT binds tighter than UNION; ORDER BY applies to the whole result.
    q = parse("SELECT 1 UNION ALL SELECT 2 INTERSECT SELECT 3 EXCEPT SELECT 4 ORDER BY 1")
    one, two, three, four = (n.Select([n.SelectItem(lit(v))]) for v in (1, 2, 3, 4))
    assert q.body == n.SetOp("EXCEPT", False, n.SetOp("UNION", True, one, n.SetOp("INTERSECT", False, two, three)), four)
    assert q.order_by == [n.OrderItem(lit(1))]


def test_subqueries():
    select = body("SELECT (SELECT max(x) FROM t2) AS m FROM (SELECT * FROM t) AS sub "
                  "WHERE id IN (SELECT id FROM t3) AND NOT EXISTS (SELECT 1 FROM t4 WHERE t4.id = sub.id)")
    assert isinstance(select.columns[0].expr, n.Subquery)
    assert isinstance(select.from_, n.SubqueryRef) and select.from_.alias == "sub"
    assert isinstance(select.where.left, n.InSubquery)
    assert select.where.right == n.Unary("NOT", n.Exists(parse("SELECT 1 FROM t4 WHERE t4.id = sub.id")))


def test_recursive_cte():
    q = parse("""
        WITH RECURSIVE chain(id, depth) AS (
            SELECT id, 0 FROM emp WHERE manager_id IS NULL
            UNION ALL
            SELECT e.id, c.depth + 1 FROM emp e JOIN chain c ON e.manager_id = c.id
        )
        SELECT * FROM chain ORDER BY depth
    """)
    assert q.with_.recursive
    cte = q.with_.ctes[0]
    assert (cte.name, cte.columns) == ("chain", ["id", "depth"])
    assert isinstance(cte.query.body, n.SetOp) and cte.query.body.all


# ---- statements --------------------------------------------------------------------


def test_create_table():
    assert parse("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
                 "score REAL DEFAULT -1.5, active BOOL)") == n.CreateTable(
        "users",
        [n.ColumnDef("id", "INTEGER"), n.ColumnDef("email", "TEXT", not_null=True, unique=True),
         n.ColumnDef("score", "REAL", default=lit(-1.5)), n.ColumnDef("active", "BOOLEAN")],
        primary_key=["id"], if_not_exists=True,
    )
    assert parse("CREATE TABLE m (a INT, b INT, PRIMARY KEY (a, b))").primary_key == ["a", "b"]


def test_other_statements():
    assert parse("CREATE UNIQUE INDEX idx ON users (email)") == n.CreateIndex("idx", "users", ["email"], unique=True)
    assert parse("DROP TABLE IF EXISTS users") == n.DropTable("users", if_exists=True)
    assert parse("DROP INDEX idx") == n.DropIndex("idx")
    assert parse("INSERT INTO t (a, b) VALUES (1, 'x'), (2, NULL)") == n.Insert(
        "t", n.Query(n.Values([[lit(1), lit("x")], [lit(2), lit(None)]])), ["a", "b"])
    assert parse("INSERT INTO t SELECT * FROM s").source.body.from_ == n.TableRef("s")
    assert parse("UPDATE t SET a = a + 1, b = 'z' WHERE id = 3") == n.Update(
        "t", [("a", n.Binary("+", col("a"), lit(1))), ("b", lit("z"))], n.Binary("=", col("id"), lit(3)))
    assert parse("DELETE FROM t") == n.Delete("t")
    assert parse("EXPLAIN ANALYZE SELECT 1") == n.Explain(parse("SELECT 1"), analyze=True)
    assert parse("ANALYZE users") == n.Analyze("users")
    assert parse_script("BEGIN; COMMIT TRANSACTION;; ROLLBACK") == [n.Begin(), n.Commit(), n.Rollback()]


COMPLEX_QUERIES = [
    # top earner per department with a window function inside a derived table
    """SELECT dept, name, salary FROM (
           SELECT dept, name, salary, RANK() OVER (PARTITION BY dept ORDER BY salary DESC) AS r FROM emp
       ) AS ranked WHERE r = 1""",
    # correlated subquery
    "SELECT name FROM emp e WHERE salary > (SELECT avg(salary) FROM emp WHERE dept = e.dept)",
    # running total and previous value
    "SELECT day, amount, sum(amount) OVER (ORDER BY day), lag(amount, 1) OVER (ORDER BY day) FROM sales",
    # multi-way join with aggregation and HAVING
    """SELECT c.name, count(o.id) AS orders, sum(i.qty * i.price) AS revenue
       FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
       JOIN items i ON i.order_id = o.id
       WHERE o.created >= '2024-01-01' GROUP BY c.name HAVING sum(i.qty * i.price) > 1000
       ORDER BY revenue DESC NULLS LAST LIMIT 5""",
    # several CTEs and a set operation
    """WITH a AS (SELECT id FROM t1), b AS (SELECT id FROM t2)
       SELECT id FROM a INTERSECT SELECT id FROM b""",
    # CASE with an operand, string concatenation and nested functions
    "SELECT CASE grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 ELSE 0 END, upper(first || ' ' || last) FROM students",
    # FULL OUTER JOIN with a USING list and a parenthesised set operation
    "(SELECT a FROM x FULL OUTER JOIN y USING (a, b)) UNION (SELECT a FROM z) LIMIT 3",
]


@pytest.mark.parametrize("sql", COMPLEX_QUERIES)
def test_complex_queries_parse(sql):
    assert isinstance(parse(sql), n.Query)


# ---- errors ----------------------------------------------------------------------


@pytest.mark.parametrize("sql,line,col,message", [
    ("SELECT * FORM users", 1, 10, "expected end of statement, got 'FORM'"),
    ("SELECT (1 + 2", 1, 14, "expected ')', got end of input"),
    ("SELECT *\nFROM users\nWHERE", 3, 6, "expected an expression, got end of input"),
    ("CREATE VIEW v AS SELECT 1", 1, 8, "Arcanum doesn't support CREATE VIEW yet"),
    ("SELECT * FROM a NATURAL JOIN b", 1, 17, "Arcanum doesn't support NATURAL joins yet"),
    ("SELECT * FROM (SELECT 1)", 1, 25, "a subquery in FROM needs an alias"),
    ("CREATE TABLE t (id INT PRIMARY KEY, x INT PRIMARY KEY)", 1, 37, "more than one primary key"),
    ("CREATE TABLE t (id UUID)", 1, 20, "expected a type"),
    ("SELECT a FROM t JOIN u", 1, 23, "expected ON or USING"),
    ("SELECT count(*) OVER w FROM t", 1, 22, "named windows"),
])
def test_errors_point_at_the_problem(sql, line, col, message):
    with pytest.raises(SqlError, match=re.escape(message)) as info:
        parse(sql)
    assert (info.value.line, info.value.col) == (line, col)


def test_error_excerpt():
    with pytest.raises(SqlError) as info:
        parse("SELECT * FORM users")
    assert info.value.excerpt() == "SELECT * FORM users\n         ^"
