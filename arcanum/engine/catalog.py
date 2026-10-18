"""Table and index definitions, and the rows stored under them.

The catalog is itself a B+tree, rooted at page 1. Each entry holds an
object's kind, name, root page and the CREATE statement that defined it;
opening a database re-parses those statements, so the schema format is
just SQL.

Storage layout:
    table  key = primary key values (or a hidden row id), value = the whole row
    index  key = indexed values + the row's table key, value = empty
Both use storage.codec, so byte order is SQL order.

ANALYZE stores statistics as catalog entries of kind "stats", one per column
plus one for the row count, so each stays well under the B+tree's entry size.
"""

import json
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property

from ..errors import ConstraintError, ExecutionError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse
from ..storage import BTree, DuplicateKeyError, decode, encode
from ..storage.btree import MAX_ENTRY
from .expr import BOOLEAN, REAL, assignable, literal_type, to_text

CATALOG_ROOT = 1
HISTOGRAM_BUCKETS = 16
HISTOGRAM_TEXT = 32  # text histogram bounds keep this many characters; enough to estimate ranges
COMMON_VALUES = 8  # most common values kept per column, so skewed equalities are estimated well


@dataclass
class ColumnSchema:
    name: str
    type: str
    not_null: bool = False
    default: object = None


@dataclass(eq=False)
class IndexSchema:
    name: str
    table: str
    columns: list  # positions in the table's rows
    unique: bool
    root: int
    sql: str


@dataclass(eq=False)
class TableSchema:
    name: str
    columns: list
    primary_key: list  # column positions; empty means rows are keyed by a hidden row id
    root: int
    sql: str
    indexes: list = field(default_factory=list)
    stats: object = None  # TableStats after ANALYZE

    @cached_property
    def booleans(self):
        """Positions of BOOLEAN columns, whose stored integers are returned as True/False."""
        return frozenset(i for i, c in enumerate(self.columns) if c.type == BOOLEAN)

    def column(self, name):
        for i, c in enumerate(self.columns):
            if c.name == name:
                return i
        return None


@dataclass
class ColumnStats:
    distinct: int
    nulls: int
    bounds: list  # equi-depth histogram: sorted values splitting the non-NULL values into equal-sized buckets
    common: list = field(default_factory=list)  # [[value, fraction of all rows]] for the most common values

    def fraction_equal(self, value, rows):
        """Estimated fraction of all rows equal to value."""
        if not rows:
            return 0.0
        for common, fraction in self.common:
            if common == value and type(common) is type(value):
                return fraction
        others = self.distinct - len(self.common)
        if others <= 0:
            return 0.0  # every value is a common value, and this is not one of them
        remaining = 1 - self.nulls / rows - sum(fraction for _, fraction in self.common)
        return max(remaining, 0.0) / others

    def fraction_below(self, value, rows):
        """Estimated fraction of all rows whose value is < value (NULLs are never counted)."""
        if not self.bounds or rows == 0:
            return 0.0
        non_null = (rows - self.nulls) / rows
        try:
            position = bisect_left(self.bounds, value)
        except TypeError:
            return 1 / 3
        if position == 0:
            return 0.0
        if position >= len(self.bounds):
            return non_null
        low, high = self.bounds[position - 1], self.bounds[position]
        within = 0.5
        if isinstance(value, (int, float)) and not isinstance(value, bool) and high != low:
            within = (value - low) / (high - low)
        return non_null * (position - 1 + within) / (len(self.bounds) - 1)


@dataclass
class TableStats:
    rows: int
    columns: list  # ColumnStats per column


class Catalog:
    def __init__(self, pager, max_keys=None):
        self.pager = pager
        self.max_keys = max_keys
        if pager.page_count == 1 and BTree.create(pager).root != CATALOG_ROOT:
            raise AssertionError("the catalog must be the first tree in a new database")
        self.tree = BTree(pager, CATALOG_ROOT)
        self.reload()

    def reload(self):
        """Rebuild the in-memory schema from the catalog tree (after open or rollback)."""
        self.tables, self.indexes, self._next_rowid = {}, {}, {}
        entries = [decode(value) for _, value in self.tree.scan()]
        stats = {}
        for kind, name, sql, root in entries:
            if kind == "table":
                self.tables[name] = _table_from_ast(parse(sql), root, sql)
        for kind, name, sql, root in entries:
            if kind == "index":
                stmt = parse(sql)
                table = self.tables[stmt.table]
                index = IndexSchema(name, table.name, [table.column(c) for c in stmt.columns], stmt.unique, root, sql)
                table.indexes.append(index)
                self.indexes[name] = index
            elif kind == "stats":
                stats.setdefault(name, {})[root] = json.loads(sql)  # root holds the column (-1 = row count)
        for name, parts in stats.items():
            table = self.tables[name]
            table.stats = TableStats(parts[-1]["rows"], [ColumnStats(**parts[i]) for i in range(len(table.columns))])

    def btree(self, root):
        return BTree(self.pager, root, self.max_keys)

    # ---- DDL -------------------------------------------------------------------

    def create_table(self, stmt, sql):
        self._check_name_free(stmt.name)
        table = _table_from_ast(stmt, None, sql)
        table.root = BTree.create(self.pager, self.max_keys).root
        self.tables[table.name] = table
        self._put_entry("table", table.name, sql, table.root)
        for column in stmt.columns:
            if column.unique:
                name = f"{table.name}_{column.name}_key"
                index_sql = f'CREATE UNIQUE INDEX "{name}" ON "{table.name}" ("{column.name}")'
                self.create_index(parse(index_sql), index_sql)
        return table

    def create_index(self, stmt, sql):
        table = self.tables.get(stmt.table)
        if table is None:
            raise SqlError(f"table {stmt.table} does not exist")
        self._check_name_free(stmt.name)
        positions = []
        for name in stmt.columns:
            pos = table.column(name)
            if pos is None:
                raise SqlError(f"column {name} does not exist in table {table.name}")
            if pos in positions:
                raise SqlError(f"column {name} appears twice in index {stmt.name}")
            positions.append(pos)
        index = IndexSchema(stmt.name, table.name, positions, stmt.unique,
                            BTree.create(self.pager, self.max_keys).root, sql)
        for key, row in list(self.scan(table)):
            self._index_insert(table, index, key, row)
        table.indexes.append(index)
        self.indexes[index.name] = index
        self._put_entry("index", index.name, sql, index.root)
        return index

    def drop_table(self, name):
        table = self.tables.pop(name)
        for index in table.indexes:
            self.btree(index.root).drop()
            self.tree.delete(encode((index.name,)))
            del self.indexes[index.name]
        self.btree(table.root).drop()
        self.tree.delete(encode((name,)))
        for column in range(-1, len(table.columns)):
            self.tree.delete(encode((name, "stats", column)))
        self._next_rowid.pop(name, None)

    def drop_index(self, name):
        index = self.indexes.pop(name)
        self.tables[index.table].indexes.remove(index)
        self.btree(index.root).drop()
        self.tree.delete(encode((name,)))

    def _check_name_free(self, name):
        if name in self.tables:
            raise SqlError(f"table {name} already exists")
        if name in self.indexes:
            raise SqlError(f"index {name} already exists")

    def _put_entry(self, kind, name, sql, root):
        self.tree.insert(encode((name,)), encode((kind, name, sql, root)))

    # ---- rows --------------------------------------------------------------------

    def analyze(self, table):
        """Collect row and column statistics for the optimizer and store them in the catalog."""
        rows = [row for _, row in self.scan(table)]
        columns = []
        for i in range(len(table.columns)):
            values = sorted(row[i] for row in rows if row[i] is not None)
            if values:
                step = (len(values) - 1) / HISTOGRAM_BUCKETS
                bounds = [values[round(b * step)] for b in range(HISTOGRAM_BUCKETS + 1)]
                bounds = [b[:HISTOGRAM_TEXT] if isinstance(b, str) else b for b in bounds]
            else:
                bounds = []
            common = [[v, n / len(rows)] for v, n in Counter(values).most_common(COMMON_VALUES)
                      if not isinstance(v, str) or len(v) <= HISTOGRAM_TEXT]
            columns.append(ColumnStats(len(set(values)), len(rows) - len(values), bounds, common))
        table.stats = TableStats(len(rows), columns)
        parts = [(-1, {"rows": len(rows)})] + [(i, vars(c)) for i, c in enumerate(columns)]
        for column, data in parts:
            self.tree.insert(encode((table.name, "stats", column)),
                             encode(("stats", table.name, json.dumps(data), column)), replace=True)

    def scan(self, table):
        """Yield (key, row) for every row, in primary key order."""
        for key, value in self.btree(table.root).scan():
            yield key, self._row(table, value)

    def scan_range(self, table, index, lo=None, hi=None, hi_inclusive=False, index_only=False):
        """Yield (table key, row) for rows whose key in `index` (None = primary key) is in [lo, hi).

        index_only builds each row from the index entry alone: the index and
        primary key columns are filled in and every other column is NULL.
        """
        if index is None:
            for key, value in self.btree(table.root).scan(lo, hi, hi_inclusive=hi_inclusive):
                yield key, self._row(table, value)
            return
        tree, width = self.btree(table.root), len(index.columns)
        positions = [*index.columns, *table.primary_key]
        for entry, _ in self.btree(index.root).scan(lo, hi, hi_inclusive=hi_inclusive):
            values = decode(entry)
            key = encode(values[width:])  # an index entry ends with the row's table key
            if index_only:
                row = [None] * len(table.columns)
                for position, value in zip(positions, values, strict=False):
                    row[position] = value
                yield key, self._typed(table, tuple(row))
            else:
                yield key, self._row(table, tree.get(key))

    def _row(self, table, value):
        return self._typed(table, decode(value))

    def _typed(self, table, row):
        if table.booleans:
            row = tuple(bool(v) if v is not None and i in table.booleans else v for i, v in enumerate(row))
        return row

    def check_row(self, table, row):
        """Apply NOT NULL and store REAL columns as floats. Returns the row to store."""
        out = []
        for column, value in zip(table.columns, row, strict=True):
            if value is None:
                if column.not_null:
                    raise ConstraintError(f"NULL value in column {column.name} of table {table.name} violates NOT NULL")
            elif column.type == REAL and not isinstance(value, float):
                value = float(value)
            out.append(value)
        return tuple(out)

    def insert_row(self, table, row, rowid_key=None):
        """Insert a row produced by check_row, maintaining every index.

        Tables without a primary key get a new row id unless rowid_key (an
        existing row's key, when UPDATE rewrites it) is given.
        """
        if table.primary_key:
            key = encode(tuple(row[i] for i in table.primary_key))
        else:
            key = rowid_key if rowid_key is not None else encode((self._take_rowid(table),))
        value = encode(row)
        _check_size(len(key) + len(value), f"row of table {table.name}")
        try:
            self.btree(table.root).insert(key, value)
        except DuplicateKeyError:
            values = ", ".join(_show(row[i]) for i in table.primary_key)
            raise ConstraintError(f"duplicate key ({values}) violates the primary key of table {table.name}") from None
        for index in table.indexes:
            self._index_insert(table, index, key, row)
        return key

    def delete_row(self, table, key, row):
        for index in table.indexes:
            self.btree(index.root).delete(encode(tuple(row[i] for i in index.columns)) + key)
        self.btree(table.root).delete(key)

    def _take_rowid(self, table):
        rowid = self._next_rowid.get(table.name)
        if rowid is None:
            last = self.btree(table.root).last_key()
            rowid = decode(last)[0] + 1 if last is not None else 1
        self._next_rowid[table.name] = rowid + 1
        return rowid

    def _index_insert(self, table, index, key, row):
        self._check_unique(table, index, row)
        entry = encode(tuple(row[i] for i in index.columns)) + key
        _check_size(len(entry), f"entry of index {index.name}")
        self.btree(index.root).insert(entry, b"")

    def _check_unique(self, table, index, row):
        values = tuple(row[i] for i in index.columns)
        if not index.unique or None in values:  # NULLs never collide in a unique index
            return
        prefix = encode(values)
        for existing, _ in self.btree(index.root).scan(lo=prefix):
            if existing.startswith(prefix):
                shown = ", ".join(_show(v) for v in values)
                raise ConstraintError(f"duplicate value ({shown}) violates unique index {index.name}")
            break


def _table_from_ast(stmt, root, sql):
    columns = []
    for c in stmt.columns:
        if any(existing.name == c.name for existing in columns):
            raise SqlError(f"column {c.name} is defined twice in table {stmt.name}")
        default = None
        if c.default is not None:
            if not isinstance(c.default, n.Literal):
                raise SqlError(f"the default of column {c.name} must be a constant")
            default = c.default.value
            if not assignable(literal_type(default), c.type):
                raise SqlError(f"default {_show(default)} does not fit column {c.name} of type {c.type}")
            if c.type == REAL and default is not None:
                default = float(default)
        columns.append(ColumnSchema(c.name, c.type, c.not_null, default))
    table = TableSchema(stmt.name, columns, [], root, sql)
    for name in stmt.primary_key:
        pos = table.column(name)
        if pos is None:
            raise SqlError(f"primary key column {name} does not exist in table {stmt.name}")
        table.primary_key.append(pos)
        columns[pos].not_null = True
    return table


def _check_size(size, what):
    limit = MAX_ENTRY - 6
    if size > limit:
        raise ExecutionError(f"{what} is {size} bytes; the limit is {limit} bytes")


def _show(value):
    return "NULL" if value is None else to_text(value)
