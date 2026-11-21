"""PostgreSQL types and functions: DATE, TIMESTAMP, INTERVAL, NUMERIC, pattern matching, aggregates with
ORDER BY and FILTER, and DISTINCT ON."""

import datetime
from decimal import Decimal

import pytest

from arcanum import Database, ExecutionError, SqlError
from arcanum.engine.datetimes import Interval


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db", max_keys=4) as database:
        database.execute_script("""
            CREATE TABLE events (id SERIAL PRIMARY KEY, name TEXT, day DATE, at TIMESTAMP, price NUMERIC(8, 2),
                                 created TIMESTAMP DEFAULT now());
            INSERT INTO events (name, day, at, price) VALUES
                ('launch', '2024-01-31', '2024-01-31 09:30:00', 19.99),
                ('review', '2024-02-29', '2024-02-29 17:05:30.25', '5'),
                ('party', DATE '2024-03-15', TIMESTAMP '2024-03-15 22:00', 120.5),
                ('nothing', NULL, NULL, NULL);
        """)
        yield database


def one(db, sql):
    return db.execute(sql).rows[0][0]


def test_dates_store_compare_and_index(db):
    assert db.execute("SELECT name FROM events WHERE day > '2024-02-01' ORDER BY day").rows == [("review",), ("party",)]
    assert one(db, "SELECT day FROM events WHERE name = 'launch'") == datetime.date(2024, 1, 31)
    db.execute("CREATE INDEX events_day ON events (day)")
    plan = db.execute("EXPLAIN SELECT name FROM events WHERE day = DATE '2024-02-29'").rows
    assert "Index" in str(plan)
    assert one(db, "SELECT name FROM events WHERE day = '2024-02-29'") == "review"
    assert one(db, "SELECT count(*) FROM events WHERE at BETWEEN '2024-02-01' AND '2024-03-31'") == 2


def test_date_arithmetic(db):
    assert one(db, "SELECT DATE '2024-01-31' + 1") == datetime.date(2024, 2, 1)
    assert one(db, "SELECT DATE '2024-03-01' - DATE '2024-02-01'") == 29
    assert one(db, "SELECT DATE '2024-01-31' + INTERVAL '1 month'") == datetime.datetime(2024, 2, 29)
    assert one(db, "SELECT TIMESTAMP '2024-01-01 10:00' - TIMESTAMP '2023-12-31 09:30'") == Interval(0, 1, 1_800_000_000)
    assert one(db, "SELECT at - interval '30 minutes' FROM events WHERE id = 1") == datetime.datetime(2024, 1, 31, 9)
    assert one(db, "SELECT interval '1 day 2 hours' * 2") == Interval(0, 2, 4 * 3_600_000_000)
    assert str(one(db, "SELECT interval '1 year 2 months 3 days 04:05:06'")) == "1 year 2 mons 3 days 04:05:06"
    assert one(db, "SELECT count(*) FROM events WHERE day >= DATE '2024-03-01' - 30") == 3  # from 2024-01-31


def test_date_functions(db):
    assert one(db, "SELECT EXTRACT(year FROM day) FROM events WHERE id = 2") == 2024
    assert one(db, "SELECT date_part('dow', DATE '2024-03-15')") == 5
    assert one(db, "SELECT EXTRACT(epoch FROM TIMESTAMP '1970-01-02')") == 86400.0
    assert one(db, "SELECT date_trunc('month', at) FROM events WHERE id = 2") == datetime.datetime(2024, 2, 1)
    assert one(db, "SELECT to_char(at, 'YYYY-MM-DD HH24:MI') FROM events WHERE id = 3") == "2024-03-15 22:00"
    assert one(db, "SELECT to_char(DATE '2024-03-05', 'FMMonth DD, YYYY')") == "March 05, 2024"
    assert one(db, "SELECT to_date('05/03/2024', 'DD/MM/YYYY')") == datetime.date(2024, 3, 5)
    assert one(db, "SELECT make_date(2024, 2, 29)") == datetime.date(2024, 2, 29)
    assert str(one(db, "SELECT age(TIMESTAMP '2024-03-15', TIMESTAMP '2022-01-10')")) == "2 years 2 mons 5 days"
    assert one(db, "SELECT CURRENT_DATE - 1 < CURRENT_DATE")
    created = one(db, "SELECT created FROM events WHERE id = 1")
    assert abs(created - datetime.datetime.now(datetime.UTC).replace(tzinfo=None)) < datetime.timedelta(minutes=5)
    assert one(db, "SELECT count(DISTINCT now()) FROM events") == 1  # one moment per statement
    with pytest.raises(ExecutionError, match="invalid input syntax for type date"):
        db.execute("INSERT INTO events (day) VALUES ('2024-02-30')")


def test_numeric_is_exact(db):
    assert one(db, "SELECT price FROM events WHERE id = 2") == Decimal("5.00")  # the column's scale is kept
    assert one(db, "SELECT sum(price) FROM events") == Decimal("145.49")
    assert one(db, "SELECT 0.1::numeric + 0.2::numeric = 0.3::numeric")
    assert one(db, "SELECT price * 3 FROM events WHERE id = 1") == Decimal("59.97")
    assert one(db, "SELECT CAST(2.345 AS NUMERIC(5, 2))") == Decimal("2.35")
    assert one(db, "SELECT round(avg(price), 2) FROM events") == Decimal("48.50")
    assert one(db, "SELECT name FROM events WHERE price = 19.99") == "launch"
    with pytest.raises(ExecutionError, match="numeric field overflow"):
        db.execute("UPDATE events SET price = 1000000 WHERE id = 1")
    db.execute("CREATE INDEX events_price ON events (price)")
    assert db.execute("SELECT name FROM events WHERE price > 10 ORDER BY price DESC").rows == [("party",), ("launch",)]


def test_pattern_matching(db):
    assert db.execute("SELECT name FROM events WHERE name ILIKE 'LA%'").rows == [("launch",)]
    assert db.execute("SELECT name FROM events WHERE name ~ '^p.r' ORDER BY id").rows == [("party",)]
    assert one(db, "SELECT count(*) FROM events WHERE name !~* 'A'") == 2  # review and nothing
    assert one(db, "SELECT regexp_replace('a1b22c', '[0-9]+', '#', 'g')") == "a#b#c"
    assert one(db, "SELECT regexp_replace('John Smith', '(\\w+) (\\w+)', '\\2, \\1')") == "Smith, John"
    with pytest.raises(ExecutionError, match="invalid regular expression"):
        db.execute("SELECT name FROM events WHERE name ~ '('")


def test_string_and_math_functions(db):
    assert db.execute("SELECT concat('a', NULL, 1), concat_ws('-', 'x', NULL, 'y'), greatest(3, 7, NULL), "
                      "least(DATE '2024-01-01', '2023-05-05'), left('abcdef', 2), right('abcdef', -2), "
                      "lpad('7', 3, '0'), split_part('a,b,c', ',', 2), initcap('hello world'), "
                      "position('lo' IN 'hello'), ceil(1.2), floor(-1.5), mod(7, 3), sign(-4)").rows == [
        ("a1", "x-y", 7, datetime.date(2023, 5, 5), "ab", "cdef", "007", "b", "Hello World", 4, 2.0, -2.0, 1, -1)]


def test_aggregates_with_order_by_and_filter(db):
    row = db.execute("""SELECT string_agg(name, ', ' ORDER BY day DESC NULLS LAST),
                               count(*) FILTER (WHERE price > 10),
                               sum(price) FILTER (WHERE day < '2024-03-01'),
                               bool_and(price > 1), bool_or(name = 'party')
                        FROM events""").rows[0]
    assert row == ("party, review, launch, nothing", 2, Decimal("24.99"), True, True)
    grouped = db.execute("SELECT EXTRACT(month FROM day) AS m, string_agg(name, '|') FROM events "
                         "WHERE day IS NOT NULL GROUP BY 1 ORDER BY 1").rows
    assert grouped == [(1, "launch"), (2, "review"), (3, "party")]
    with pytest.raises(SqlError, match="ORDER BY and FILTER only apply to aggregate"):
        db.execute("SELECT lower(name) FILTER (WHERE true) FROM events")


def test_distinct_on(tmp_path):
    with Database(tmp_path / "d.db") as db:
        db.execute_script("""
            CREATE TABLE readings (sensor TEXT, at INTEGER, value REAL);
            INSERT INTO readings VALUES ('a', 1, 1.0), ('a', 3, 3.0), ('b', 2, 2.0), ('a', 2, 2.5), ('b', 5, 5.5);
        """)
        latest = db.execute("SELECT DISTINCT ON (sensor) sensor, at, value FROM readings ORDER BY sensor, at DESC")
        assert latest.rows == [("a", 3, 3.0), ("b", 5, 5.5)]
        first = db.execute("SELECT DISTINCT ON (1) sensor, value FROM readings ORDER BY 1, value")
        assert first.rows == [("a", 1.0), ("b", 2.0)]


def test_types_survive_reopen_and_analyze(tmp_path):
    path = tmp_path / "r.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (d DATE PRIMARY KEY, amount NUMERIC(10, 3), at TIMESTAMP)")
        db.execute("INSERT INTO t VALUES ('2024-05-01', 1.5, '2024-05-01 12:00:00.5'), ('2023-01-01', -2, NULL)")
        db.execute("ANALYZE t")
    with Database(path) as db:
        assert db.execute("SELECT * FROM t ORDER BY d").rows == [
            (datetime.date(2023, 1, 1), Decimal("-2.000"), None),
            (datetime.date(2024, 5, 1), Decimal("1.500"), datetime.datetime(2024, 5, 1, 12, 0, 0, 500000))]
        assert db.catalog.tables["t"].stats.columns[0].bounds[0] == datetime.date(2023, 1, 1)
        db.execute("CREATE TABLE spans (i INTERVAL PRIMARY KEY)")
        db.execute("INSERT INTO spans VALUES ('1 day'), ('2 hours'), ('1 month')")
        assert [str(v) for (v,) in db.execute("SELECT i FROM spans ORDER BY i DESC").rows] == [
            "1 mon", "1 day", "02:00:00"]
