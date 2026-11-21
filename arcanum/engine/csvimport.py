"""CSV import: rows from a CSV text into a new table (column types inferred) or an existing one.

The import is planned first (plan_import): the columns, their types and every
value checked against its column, so a bad value is reported with its row and
column before anything is written. The plan then becomes ordinary SQL
(import_sql), a CREATE TABLE when the table is new and one INSERT for all the
rows, so the import runs, is traced and can be watched like any statement,
and the INSERT is atomic: either every row arrives or none does.

As in PostgreSQL's CSV format, an empty unquoted field is NULL. Values are
written as SQL literals with quotes doubled, so no CSV content is ever read
as SQL.
"""

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..errors import SqlError
from ..sql.parser import RESERVED
from .expr import BOOLEAN, DATE, DECIMAL, INTEGER, REAL, TEXT, TIMESTAMP

MAX_COLUMNS = 100
PREVIEW_ROWS = 5
DELIMITERS = ",;\t|"
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_TRUE, _FALSE = {"true", "t", "yes", "y"}, {"false", "f", "no", "n"}
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_INT_RANGE = (-(2**63), 2**63 - 1)


@dataclass
class ImportPlan:
    table: str
    create: bool  # whether the table is new
    columns: list  # [(name, type)] of the table's columns the CSV fills, in CSV order
    rows: list  # [[SQL literal]], one list per CSV row
    sample: list  # the first rows as read, for a preview

    def preview(self):
        return {"table": self.table, "create": self.create, "rows_total": len(self.rows),
                "columns": [{"name": name, "type": kind} for name, kind in self.columns], "sample": self.sample}


def plan_import(catalog, table, text, header=True):
    """Read the CSV and check it against `table` (created with inferred types if it does not exist)."""
    if not _NAME.fullmatch(table) or table.upper() in RESERVED:
        raise SqlError(f"{table!r} cannot be the table name: use lowercase letters, digits and _ (not first), "
                       "and not a reserved word")
    records = _read(text)
    if header:
        names, records = records[0], records[1:]
    else:
        names = [f"column{i + 1}" for i in range(len(records[0]))]
    width = len(names)
    if width > MAX_COLUMNS:
        raise SqlError(f"the CSV has {width} columns; at most {MAX_COLUMNS} can be imported")
    for number, record in enumerate(records, 2 if header else 1):
        if len(record) != width:
            raise SqlError(f"line {number} has {len(record)} fields, but the CSV has {width} columns")
    existing = catalog.tables.get(table)
    if existing is None:
        names = _column_names(names)
        columns = [(name, _infer([r[i] for r in records])) for i, name in enumerate(names)]
    else:
        columns = _match(existing, names, header)
    rows = [[_literal(value, kind, number, name) for value, (name, kind) in zip(record, columns, strict=True)]
            for number, record in enumerate(records, 2 if header else 1)]
    return ImportPlan(table, existing is None, columns, rows, records[:PREVIEW_ROWS])


def import_sql(plan):
    """The plan as SQL: CREATE TABLE (when new) and one INSERT with every row."""
    statements = []
    if plan.create:
        statements.append(f"CREATE TABLE {plan.table} ({', '.join(f'{n} {t}' for n, t in plan.columns)})")
    if plan.rows:
        values = ",\n  ".join(f"({', '.join(row)})" for row in plan.rows)
        statements.append(f"INSERT INTO {plan.table} ({', '.join(n for n, _ in plan.columns)}) VALUES\n  {values}")
    return ";\n".join(statements) + ";"


def _read(text):
    if not text.strip():
        raise SqlError("the CSV is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=DELIMITERS)
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    try:
        records = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter, strict=True) if r]
    except csv.Error as e:
        raise SqlError(f"the CSV cannot be read: {e}") from None
    return records


def _column_names(raw):
    """Header fields as plain lowercase identifiers, made unique."""
    names = []
    for i, field in enumerate(raw):
        name = re.sub(r"[^a-z0-9_]+", "_", field.strip().lower()).strip("_")[:63] or f"column{i + 1}"
        if name[0].isdigit() or name.upper() in RESERVED:
            name = f"c_{name}"[:63]
        base, k = name, 2
        while name in names:
            name, k = f"{base}_{k}", k + 1
        names.append(name)
    return names


def _match(table, names, header):
    """The existing table's columns the CSV fills: by header name, or all of them in order."""
    if not header:
        if len(names) != len(table.columns):
            raise SqlError(f"the CSV has {len(names)} columns and {table.name} has {len(table.columns)}; "
                           "add a header row naming the columns to fill")
        return [(c.name, c.type) for c in table.columns]
    columns = []
    for field in names:
        position = table.column(field.strip().lower())
        if position is None:
            raise SqlError(f"{table.name} has no column {field.strip()!r} (from the CSV header)")
        column = table.columns[position]
        if column.name in (n for n, _ in columns):
            raise SqlError(f"the CSV header names {column.name} twice")
        columns.append((column.name, column.type))
    return columns


def _infer(values):
    present = [v.strip() for v in values if v.strip() != ""]
    if not present:
        return TEXT
    for kind, test in ((INTEGER, _is_int), (REAL, _is_float), (BOOLEAN, _is_bool), (DATE, _is_date),
                       (TIMESTAMP, _is_timestamp)):
        if all(test(v) for v in present):
            return kind
    return TEXT


def _is_int(v):
    return bool(_INT.fullmatch(v)) and _INT_RANGE[0] <= int(v) <= _INT_RANGE[1]


def _is_float(v):
    return bool(_FLOAT.fullmatch(v)) and math.isfinite(float(v))


def _is_bool(v):
    return v.lower() in _TRUE | _FALSE


def _is_date(v):
    try:
        return len(v) == 10 and date.fromisoformat(v) is not None
    except ValueError:
        return False


def _is_timestamp(v):
    try:
        return datetime.fromisoformat(v).tzinfo is None
    except ValueError:
        return False


def _literal(value, kind, row, column):
    """One CSV field as an SQL literal for a column of type `kind`."""
    if value == "":
        return "NULL"
    v = value.strip()
    checks = {INTEGER: _is_int, REAL: _is_float, BOOLEAN: _is_bool}
    if kind in checks:
        if not checks[kind](v):
            raise SqlError(f"line {row}, column {column}: {value!r} is not {kind}")
        if kind == BOOLEAN:
            return "TRUE" if v.lower() in _TRUE else "FALSE"
        return str(int(v)) if kind == INTEGER else repr(float(v))  # the forms the SQL lexer reads
    if kind == DECIMAL:
        try:
            if not Decimal(v).is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise SqlError(f"line {row}, column {column}: {value!r} is not NUMERIC") from None
        return v
    return "'" + value.replace("'", "''") + "'"  # text, and everything the column converts from text
