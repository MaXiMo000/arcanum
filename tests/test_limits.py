"""Statements that try to exhaust memory or time fail fast with a clear error instead of hurting the server."""

import pytest

from arcanum import Database, ExecutionError, StatementTimeoutError
from arcanum.engine.executor import QueryLimits


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "l.db", limits=QueryLimits(timeout=1)) as database:
        yield database


@pytest.mark.parametrize("sql,message", [
    ("SELECT repeat('x', 2000000000)", "repeat's result would be 2,000,000,000 long"),
    ("SELECT lpad('x', 2000000000, 'ab')", "the padded text would be"),
    ("SELECT replace(repeat('a', 1000), 'a', repeat('b', 100000))", "replace's result would be"),
    ("SELECT length(string_agg(repeat('x', 1000000), ',')) FROM generate_series(1, 60)", "string_agg's result"),
    ("WITH RECURSIVE r(s) AS (SELECT 'ab' UNION ALL SELECT s || s FROM r WHERE length(s) < 1000000000) "
     "SELECT max(length(s)) FROM r", "the result would be"),
    ("SELECT '" + "[" * 5000 + "]" * 5000 + "'::jsonb", "nested too deeply"),
    ("SELECT '1e999999'::numeric * '1e999999'::numeric", "out of range"),
    ("SELECT 10 ^ 400", "cannot raise"),
])
def test_values_have_limits(db, sql, message):
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_statements_time_out(db):
    with pytest.raises(StatementTimeoutError, match="statement timeout"):
        db.execute("SELECT count(*) FROM generate_series(1, 1000000000)")
    assert db.execute("SELECT 1").rows == [(1,)]  # the database is fine afterwards


def test_power_operator(db):
    assert db.execute("SELECT 2 ^ 10, 2 ^ 3 ^ 2, 2.0::numeric ^ 3").rows == [(1024.0, 64.0, 8)]
