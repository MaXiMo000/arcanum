"""CSV import: type inference, NULLs and quoting, existing tables, and refusing bad input before writing."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from arcanum import Database
from arcanum.engine.csvimport import import_sql, plan_import
from arcanum.errors import ExecutionError, SqlError
from arcanum.sql.parser import parse_statements


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db") as database:
        yield database


def test_a_new_table_gets_inferred_types_and_every_value(db):
    text = ('Name,Age,Score,Joined,Active,Note,Seen At\n'
            'Ada,36,9.5,2024-01-02,true,"says ""hi"", then; leaves",2024-01-02 10:00:00\n'
            "Grace,,1e20,2023-12-31,f,,2024-01-02T11:30:00\n"
            "O'Brien,-7,-.5,,yes,\"two\nlines\",\n")
    assert [r.status for r in db.import_csv("people", text)] == ["CREATE TABLE", "INSERT 3"]
    assert db.catalog.tables["people"].sql == ("CREATE TABLE people (name TEXT, age INTEGER, score REAL, joined DATE, "
                                               "active BOOLEAN, note TEXT, seen_at TIMESTAMP)")
    assert db.execute("SELECT * FROM people").rows == [
        ("Ada", 36, 9.5, date(2024, 1, 2), True, 'says "hi", then; leaves', datetime(2024, 1, 2, 10)),
        ("Grace", None, 1e20, date(2023, 12, 31), False, None, datetime(2024, 1, 2, 11, 30)),
        ("O'Brien", -7, -0.5, None, True, "two\nlines", None),
    ]


def test_header_names_become_plain_unique_identifiers(db):
    db.import_csv("odd", "Order Id,order id,1st,select,,é\n1,2,3,4,5,6\n")
    assert [c.name for c in db.catalog.tables["odd"].columns] == [
        "order_id", "order_id_2", "c_1st", "c_select", "column5", "column6"]


def test_an_existing_table_is_filled_by_header_or_position(db):
    db.execute("CREATE TABLE d (k INTEGER, day DATE, amount NUMERIC, tag TEXT)")
    db.import_csv("d", "tag,k\nx,1\n")
    db.import_csv("d", "2;2024-02-29;10.50;y\n", header=False)  # semicolons, sniffed
    assert db.execute("SELECT * FROM d ORDER BY k").rows == [
        (1, None, None, "x"), (2, date(2024, 2, 29), Decimal("10.5"), "y")]


@pytest.mark.parametrize(("table", "text", "header", "message"), [
    ("d", "k\nold\n", True, "line 2, column k: 'old' is not INTEGER"),
    ("d", "nope\n1\n", True, "no column 'nope'"),
    ("d", "k,k\n1,2\n", True, "names k twice"),
    ("d", "1,2,3\n", False, "add a header row"),
    ("t2", "a,b\n1\n", True, "line 2 has 1 fields"),
    ("Bad", "a\n1\n", True, "cannot be the table name"),
    ("select", "a\n1\n", True, "reserved word"),
    ("t3", "   \n", True, "empty"),
    ("t4", ",".join(f"c{i}" for i in range(101)) + "\n", True, "at most 100"),
])
def test_bad_input_is_refused_before_anything_is_written(db, table, text, header, message):
    db.execute("CREATE TABLE d (k INTEGER, v TEXT)")
    with pytest.raises(SqlError, match=message):
        db.import_csv(table, text, header)
    assert set(db.catalog.tables) == {"d"} and db.execute("SELECT count(*) FROM d").rows == [(0,)]


def test_a_value_the_column_rejects_leaves_no_rows(db):
    db.execute("CREATE TABLE d (k INTEGER PRIMARY KEY, day DATE)")
    with pytest.raises(ExecutionError, match="2024-02-30"):
        db.import_csv("d", "k,day\n1,2024-02-28\n2,2024-02-30\n")
    assert db.execute("SELECT count(*) FROM d").rows == [(0,)]  # one INSERT: all rows or none


def test_csv_text_never_becomes_sql(db):
    sneaky = "v\n\"x'); DROP TABLE d; --\"\n"
    db.execute("CREATE TABLE d (v TEXT)")
    db.import_csv("d", sneaky)
    assert db.execute("SELECT v FROM d").rows == [("x'); DROP TABLE d; --",)]
    plan = plan_import(db.catalog, "d", sneaky)
    assert len(parse_statements(import_sql(plan))) == 1  # the quoted semicolon ends nothing
