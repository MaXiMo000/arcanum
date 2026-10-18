"""The query advisor's hints and the error doctor's fixes."""

import pytest

from arcanum import Database
from arcanum.engine.doctor import diagnose
from arcanum.errors import ConstraintError, SqlError


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db") as database:
        database.execute_script("""
            CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c REAL);
            CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER, note TEXT);
            CREATE INDEX t_a ON t (a);
            INSERT INTO t SELECT n, n % 50, 'x' || n, n * 1.5 FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 2000) SELECT n FROM s) AS q;
            INSERT INTO u SELECT n, n % 300, 'n' || n FROM
                (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 600) SELECT n FROM s) AS q;
            ANALYZE;
        """)
        yield database


def hints(db, sql):
    return {h["code"]: h for h in db.execute(sql, trace=True).advice}


@pytest.mark.parametrize(("sql", "code"), [
    ("SELECT * FROM t WHERE a = 7", "select-star"),
    ("SELECT id, b FROM t WHERE a = 7", "covering-index"),
    ("SELECT id FROM t WHERE a = 7", "index-only"),
    ("SELECT * FROM t WHERE b = NULL", "null-compare"),
    ("SELECT * FROM t WHERE b LIKE '%99'", "leading-wildcard"),
    ("SELECT * FROM t WHERE a + 1 = 8", "hidden-column"),
    ("SELECT count(*) FROM t, u", "cartesian"),
    ("SELECT count(*) FROM t JOIN u ON t.id < u.t_id", "nested-loop"),
    ("SELECT a FROM t UNION SELECT t_id FROM u", "union-distinct"),
    ("SELECT a, count(*) FROM t GROUP BY a HAVING a > 3", "having-without-aggregate"),
    ("SELECT * FROM t ORDER BY id LIMIT 5 OFFSET 1500", "large-offset"),
    ("SELECT id FROM t WHERE id NOT IN (SELECT t_id FROM u)", "not-in-null"),
    ("SELECT id, (SELECT max(note) FROM u WHERE u.t_id > t.id) FROM t WHERE id < 30", "correlated"),
    ("DELETE FROM u", "no-where"),
])
def test_each_rule_fires(db, sql, code):
    assert code in hints(db, sql)


def test_a_missing_index_fix_makes_the_query_cheaper(db):
    sql = "SELECT * FROM t WHERE c > 2990"
    hint = hints(db, sql)["missing-index"]
    assert hint["level"] == "warn" and hint["fix"]["sql"] == "CREATE INDEX t_c ON t (c)"
    db.execute(hint["fix"]["sql"])
    assert "missing-index" not in hints(db, sql)
    assert "IndexScan t using t_c" in "\n".join(r[0] for r in db.execute("EXPLAIN " + sql).rows)


def test_missing_index_advice_covers_the_columns_read(db):
    sql = "SELECT id, b FROM t WHERE c > 2990"
    fix = hints(db, sql)["missing-index"]["fix"]["sql"]
    assert fix == "CREATE INDEX t_c_b ON t (c, b)"
    before = db.execute(sql, trace=True)
    db.execute(fix)
    after = db.execute(sql, trace=True)
    assert after.rows == before.rows and after.plan["children"][0]["operator"] == "IndexOnlyScan"
    assert [h["code"] for h in after.advice] == ["index-only"]


def test_rewrites_keep_the_statement_and_fix_it(db):
    fix = hints(db, "SELECT * FROM t WHERE b = NULL OR a != NULL")["null-compare"]["fix"]
    assert fix["sql"] == "SELECT * FROM t WHERE b IS NULL OR a IS NOT NULL"
    assert hints(db, "SELECT a FROM t UNION SELECT t_id FROM u")["union-distinct"]["fix"]["sql"].count("UNION ALL") == 1


def test_hints_point_at_plan_nodes_and_unanalyzed_tables_get_analyze(tmp_path):
    with Database(tmp_path / "t.db") as db:
        db.execute_script("CREATE TABLE r (id INTEGER PRIMARY KEY, v INTEGER); CREATE INDEX r_v ON r (v);")
        db.execute("INSERT INTO r SELECT n, n % 400 FROM (WITH RECURSIVE s(n) AS "
                   "(SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 1200) SELECT n FROM s) AS q")
        result = db.execute("SELECT * FROM r WHERE v > 397", trace=True)
        found = {h["code"]: h for h in result.advice}
        assert found["analyze"]["fix"]["sql"] == "ANALYZE r"
        ids = set()
        stack = [result.plan]
        while stack:
            node = stack.pop()
            ids.add(node["id"])
            stack.extend(node["children"])
        assert found["analyze"]["node"] in ids


def test_untraced_statements_carry_no_advice(db):
    assert db.execute("SELECT * FROM t WHERE b = NULL").advice == []


# ---- the doctor ------------------------------------------------------------------------------


def doctor(db, sql):
    with pytest.raises((SqlError, ConstraintError)) as caught:
        db.execute_script(sql)
    return diagnose(caught.value, db.catalog, sql)


@pytest.mark.parametrize(("sql", "fixed"), [
    ("SELECT * FROM tt", "SELECT * FROM t"),
    ("SELECT nte FROM u", "SELECT note FROM u"),
    ("SELECT * FROM u WHERE note = \"n5\"", "SELECT * FROM u WHERE note = 'n5'"),
    ("SELCT * FROM t", "SELECT * FROM t"),
    ("SELECT * FORM t", "SELECT * FROM t"),
    ("SELECT note FORM u", "SELECT note FROM u"),
    ("SELECT * FROM t WHRE id = 1", "SELECT * FROM t WHERE id = 1"),
    ("SELECT note, FROM u", "SELECT note FROM u"),
    ("SELECT * FROM t WHERE id == 1", "SELECT * FROM t WHERE id = 1"),
    ("SELECT * FROM u WHERE note = 'n5", "SELECT * FROM u WHERE note = 'n5'"),
    ("SELECT count(* FROM t", "SELECT count(*) FROM t"),
    ("SELECT (1 + 2", "SELECT (1 + 2)"),
    ("SELECT a, count(*) FROM t", "SELECT a, count(*) FROM t GROUP BY a"),
    ("SELECT a, b, count(*) FROM t GROUP BY a ORDER BY a", "SELECT a, b, count(*) FROM t GROUP BY a, b ORDER BY a"),
    ("SELECT uper(b) FROM t", "SELECT upper(b) FROM t"),
    ("SELECT * t", "SELECT * FROM t"),
    ("SELECT id note t_id FROM u", "SELECT id, note, t_id FROM u"),
    ("SELECT * FROM u WHERE note = ‘n5’", "SELECT * FROM u WHERE note = 'n5'"),
    ("CREATE TABLE z (a INTEGR)", "CREATE TABLE z (a INTEGER)"),
    ("SELECT * FROM t x WHERE x.bb = 'x'", "SELECT * FROM t x WHERE x.b = 'x'"),
    ("UPDATE u SET nte = 'x'", "UPDATE u SET note = 'x'"),
])
def test_doctor_fixes_common_mistakes(db, sql, fixed):
    found = doctor(db, sql)
    assert found["fix"] == fixed, found
    assert found["hint"] and found["fix_label"]


@pytest.mark.parametrize(("sql", "words"), [
    ("SELECT id FROM t JOIN u ON t.id = u.t_id", "t.id or u.id"),
    ("SELECT * FROM t WHERE count(*) > 1", "HAVING"),
    ("INSERT INTO u VALUES (5, 1)", "id, t_id, note"),
    ("INSERT INTO u VALUES (5, 1, 'x')", "primary key"),
    ("SELECT * FROM", "ends too early"),
    ("COMMIT", "BEGIN"),
])
def test_doctor_explains_what_it_cannot_fix(db, sql, words):
    found = doctor(db, sql)
    assert words in found["hint"] and found["fix"] is None


def test_doctor_marks_the_faulty_characters(db):
    sql = "SELECT * FROM t WHERE b = 'x' AND nope = 1"
    found = doctor(db, sql)
    assert sql[found["pos"]:found["end"]] == "nope"
