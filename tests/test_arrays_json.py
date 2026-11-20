"""Arrays, JSONB, set-returning functions with LATERAL, and ROLLUP / CUBE / GROUPING SETS."""

import datetime
from decimal import Decimal

import pytest

from arcanum import Database, ExecutionError, SqlError
from arcanum.values import Jsonb


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "j.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, tags TEXT[], scores INTEGER[], meta JSONB);
            INSERT INTO posts VALUES
                (1, 'intro', '{sql,db}', ARRAY[3, 5], '{"author": "ann", "likes": 10, "draft": false}'),
                (2, 'trees', ARRAY['db', 'btree'], '{1}', '{"author": "bo", "likes": 4, "extra": {"pages": [1, 2]}}'),
                (3, 'empty', '{}', NULL, NULL);
        """)
        yield database


def rows(db, sql):
    return db.execute(sql).rows


def one(db, sql):
    return rows(db, sql)[0][0]


def test_arrays_store_index_and_compare(db):
    assert one(db, "SELECT tags FROM posts WHERE id = 1") == ("sql", "db")
    assert one(db, "SELECT tags[2] FROM posts WHERE id = 2") == "btree"
    assert one(db, "SELECT tags[9] FROM posts WHERE id = 2") is None
    assert rows(db, "SELECT id FROM posts WHERE 'db' = ANY(tags) ORDER BY id") == [(1,), (2,)]
    assert rows(db, "SELECT id FROM posts WHERE tags @> ARRAY['db', 'sql']") == [(1,)]
    assert rows(db, "SELECT id FROM posts WHERE tags && '{btree,x}'") == [(2,)]
    assert rows(db, "SELECT id FROM posts WHERE 2 < ALL(scores) ORDER BY id") == [(1,)]
    assert one(db, "SELECT ARRAY[1, 2] || 3 || ARRAY[4]") == (1, 2, 3, 4)
    assert one(db, "SELECT array_length(tags, 1) FROM posts WHERE id = 1") == 2
    assert one(db, "SELECT cardinality(tags) FROM posts WHERE id = 3") == 0
    assert one(db, "SELECT array_to_string(array_append(tags, 'x'), '-') FROM posts WHERE id = 1") == "sql-db-x"
    assert one(db, "SELECT string_to_array('a,b,,c', ',')") == ("a", "b", "", "c")
    assert one(db, "SELECT array_position(ARRAY['a', 'b'], 'b')") == 2
    assert one(db, "SELECT '{1,NULL,3}'::integer[]") == (1, None, 3)
    assert one(db, "SELECT CAST(ARRAY[1, 2] AS TEXT)") == "{1,2}"
    assert rows(db, "SELECT id FROM posts ORDER BY scores DESC NULLS LAST, id") == [(1,), (2,), (3,)]
    db.execute("CREATE INDEX posts_tags ON posts (tags)")
    assert rows(db, "SELECT id FROM posts WHERE tags = ARRAY['db', 'btree']") == [(2,)]
    with pytest.raises(SqlError, match="empty array"):
        db.execute("SELECT ARRAY[]")
    assert one(db, "SELECT ARRAY[]::integer[]") == ()


def test_array_agg_and_unnest(db):
    assert one(db, "SELECT array_agg(title ORDER BY id DESC) FROM posts") == ("empty", "trees", "intro")
    assert one(db, "SELECT array_agg(scores[1] ORDER BY id) FROM posts") == (3, 1, None)  # NULLs are kept
    tag_counts = rows(db, """SELECT tag, count(*) FROM posts, unnest(tags) AS tag
                             GROUP BY tag ORDER BY count(*) DESC, tag""")
    assert tag_counts == [("db", 2), ("btree", 1), ("sql", 1)]
    assert rows(db, "SELECT * FROM unnest(ARRAY[1, 2], ARRAY['a']) AS t(n, s)") == [(1, "a"), (2, None)]


def test_jsonb(db):
    assert one(db, "SELECT meta->>'author' FROM posts WHERE id = 1") == "ann"
    assert one(db, "SELECT meta->'likes' FROM posts WHERE id = 1") == Jsonb(10)
    assert one(db, "SELECT (meta->>'likes')::integer + 1 FROM posts WHERE id = 2") == 5
    assert one(db, "SELECT (meta->'likes')::numeric FROM posts WHERE id = 2") == Decimal(4)
    assert one(db, "SELECT meta #>> '{extra,pages,1}' FROM posts WHERE id = 2") == "2"
    assert one(db, "SELECT meta['extra']['pages'] FROM posts WHERE id = 2") == Jsonb([1, 2])
    assert rows(db, """SELECT id FROM posts WHERE meta @> '{"author": "bo"}'""") == [(2,)]
    assert rows(db, "SELECT id FROM posts WHERE meta ? 'draft'") == [(1,)]
    assert rows(db, "SELECT id FROM posts WHERE meta ?| ARRAY['extra', 'nope']") == [(2,)]
    assert str(one(db, """SELECT '{"b": 1, "a": [true, null]}'::jsonb""")) == '{"a": [true, null], "b": 1}'
    assert one(db, "SELECT jsonb_build_object('n', 1, 'd', DATE '2024-01-02', 't', ARRAY[1, 2], 'x', NULL)") == \
        Jsonb({"n": 1, "d": "2024-01-02", "t": [1, 2], "x": None})
    assert one(db, "SELECT jsonb_agg(title ORDER BY id) FROM posts") == Jsonb(["intro", "trees", "empty"])
    assert one(db, "SELECT jsonb_object_agg(title, id) FROM posts WHERE id < 3") == Jsonb({"intro": 1, "trees": 2})
    assert one(db, "SELECT jsonb_set(meta, '{likes}', '11') ->> 'likes' FROM posts WHERE id = 1") == "11"
    assert one(db, "SELECT (meta - 'draft') ? 'draft' FROM posts WHERE id = 1") is False
    assert one(db, """SELECT meta || '{"x": 1}' FROM posts WHERE id = 2""") == Jsonb(
        {"author": "bo", "likes": 4, "extra": {"pages": [1, 2]}, "x": 1})
    assert one(db, "SELECT jsonb_typeof(meta->'extra') FROM posts WHERE id = 2") == "object"
    assert rows(db, "SELECT key, value FROM posts, jsonb_each_text(meta) WHERE id = 1 ORDER BY key") == [
        ("author", "ann"), ("draft", "false"), ("likes", "10")]
    assert rows(db, "SELECT count(*) FROM posts, jsonb_each(meta)") == [(6,)]
    assert rows(db, """SELECT value FROM jsonb_array_elements_text('["a", 1]')""") == [("a",), ("1",)]
    with pytest.raises(ExecutionError, match="invalid input syntax for type jsonb"):
        db.execute("SELECT '{bad'::jsonb")


def test_lateral_and_generate_series(db):
    assert rows(db, "SELECT * FROM generate_series(1, 7, 3)") == [(1,), (4,), (7,)]
    days = rows(db, "SELECT d FROM generate_series(DATE '2024-02-27', DATE '2024-03-01', interval '1 day') AS d")
    assert [d for (d,) in days] == [datetime.datetime(2024, 2, d) for d in (27, 28, 29)] + [
        datetime.datetime(2024, 3, 1)]
    top = rows(db, """SELECT p.id, s.v FROM posts p
                      CROSS JOIN LATERAL (SELECT v FROM unnest(p.scores) AS v ORDER BY v DESC LIMIT 1) s
                      ORDER BY p.id""")
    assert top == [(1, 5), (2, 1)]
    with_missing = rows(db, """SELECT p.id, s.v FROM posts p
                               LEFT JOIN LATERAL (SELECT v FROM unnest(p.scores) AS v WHERE v > 2) s ON true
                               ORDER BY p.id, s.v""")
    assert with_missing == [(1, 3), (1, 5), (2, None), (3, None)]
    with pytest.raises(SqlError, match="returns rows"):
        db.execute("SELECT generate_series(1, 3)")


def test_grouping_sets(tmp_path):
    with Database(tmp_path / "g.db") as db:
        db.execute_script("""
            CREATE TABLE sales (region TEXT, product TEXT, amount INTEGER);
            INSERT INTO sales VALUES ('north', 'a', 10), ('north', 'b', 20), ('south', 'a', 5);
        """)
        rollup = rows(db, """SELECT region, product, sum(amount), grouping(region, product) FROM sales
                            GROUP BY ROLLUP (region, product) ORDER BY region NULLS LAST, product NULLS LAST""")
        assert rollup == [("north", "a", 10, 0), ("north", "b", 20, 0), ("north", None, 30, 1),
                          ("south", "a", 5, 0), ("south", None, 5, 1), (None, None, 35, 3)]
        cube = rows(db, "SELECT region, product, sum(amount) FROM sales GROUP BY CUBE (region, product)")
        assert len(cube) == 3 + 2 + 2 + 1
        assert (None, "a", 15) in cube
        sets = rows(db, """SELECT region, product, count(*) FROM sales GROUP BY GROUPING SETS ((region), (product))
                          HAVING count(*) > 1 ORDER BY 1 NULLS LAST, 2 NULLS LAST""")
        assert sets == [("north", None, 2), (None, "a", 2)]
        assert rows(db, "SELECT sum(amount) FROM sales WHERE false GROUP BY ROLLUP (region)") == [(None,)]
