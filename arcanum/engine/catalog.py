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
from decimal import Decimal
from functools import cached_property

from ..errors import ConstraintError, ExecutionError, LockWaitError, SerializationError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse
from ..storage import BTree, decode, encode
from ..storage.btree import MAX_ENTRY
from .expr import (
    BOOLEAN,
    INTEGER,
    INTERVAL,
    REAL,
    TEXT,
    assignable,
    cast,
    literal_type,
    scale_decimal,
    to_text,
)
from .mvcc import FROZEN, pack, prune, settled, unpack

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
    identity: str | None = None  # "ALWAYS" or "BY DEFAULT": filled from the column's sequence
    default_expr: object = None  # a default that is an expression, such as now(): evaluated per row
    precision: int | None = None  # NUMERIC(precision, scale)
    scale: int | None = None
    default_sql: str | None = None  # the default as written


@dataclass(eq=False)
class ForeignKeySchema:
    name: str
    table: str  # the referencing (child) table
    columns: list  # positions in the child's rows
    parent: str
    ref_columns: list  # column names in the parent; empty means its primary key
    on_delete: str
    on_update: str


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
    checks: list = field(default_factory=list)  # [nodes.Check], each with its name
    foreign_keys: list = field(default_factory=list)  # [ForeignKeySchema]

    @cached_property
    def booleans(self):
        """Positions of BOOLEAN columns, whose stored integers are returned as True/False."""
        return frozenset(i for i, c in enumerate(self.columns) if c.type == BOOLEAN)

    @cached_property
    def scales(self):
        """{position: scale} of NUMERIC(p, s) columns: stored values lose trailing zeros, reads restore them."""
        return {i: c.scale for i, c in enumerate(self.columns) if c.scale is not None}

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
        self.mvcc = None  # the TransactionManager, set by the Database
        self.unsettled = {}  # table name -> keys of rows that may hold versions other than one frozen one
        self.reload()

    def reload(self):
        """Rebuild the in-memory schema from the catalog tree (after open or rollback).

        Rows changed since the last prune (unsettled) are tracked in memory and
        kept across a reload: a rolled-back statement can only have removed some.
        """
        self.tables, self.indexes, self._next_rowid = {}, {}, {}
        self.views = {}  # name -> CREATE VIEW text, expanded wherever the view is used
        self.meta, self.clog = {}, set()
        entries = [decode(value) for _, value in self.tree.scan()]
        stats = {}
        for kind, name, sql, root in entries:
            if kind == "table":
                self.tables[name] = _table_from_ast(parse(sql), root, sql)
            elif kind == "view":
                self.views[name] = sql
            elif kind == "meta":
                self.meta[name] = root
            elif kind == "clog":
                self.clog.add(root)
        for kind, name, sql, root in entries:
            if kind == "index":
                stmt = parse(sql)
                table = self.tables[stmt.table]
                index = IndexSchema(name, table.name, [table.column(c) for c in stmt.columns], stmt.unique, root, sql)
                table.indexes.append(index)
                self.indexes[name] = index
            elif kind == "stats":
                stats.setdefault(name, {})[root] = json.loads(sql, object_hook=_stat_value)  # root: column, -1: rows
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
        uniques = [(None, [c.name]) for c in stmt.columns if c.unique] + list(stmt.uniques)
        for name, columns in uniques:
            name = name or f"{table.name}_{'_'.join(columns)}_key"
            listed = ", ".join(f'"{c}"' for c in columns)
            index_sql = f'CREATE UNIQUE INDEX "{name}" ON "{table.name}" ({listed})'
            self.create_index(parse(index_sql), index_sql)
        for fk in table.foreign_keys:
            self.validate_foreign_key(table, fk)
        return table

    def validate_foreign_key(self, table, fk):
        parent = self.tables.get(fk.parent)
        if parent is None:
            raise SqlError(f"table {fk.parent} referenced by foreign key {fk.name} does not exist")
        refs = self.referenced(fk)
        if len(refs) != len(fk.columns):
            raise SqlError(f"foreign key {fk.name} has {len(fk.columns)} columns but references {len(refs)}")
        if refs != parent.primary_key and not any(i.unique and i.columns == refs for i in parent.indexes):
            raise SqlError(f"there is no unique constraint matching the columns of table {parent.name} "
                           f"referenced by foreign key {fk.name}")
        for child, ref in zip(fk.columns, refs, strict=True):
            a, b = table.columns[child], parent.columns[ref]
            if not (assignable(a.type, b.type) and assignable(b.type, a.type)):
                raise SqlError(f"foreign key {fk.name}: column {a.name} ({a.type}) and {parent.name}.{b.name} "
                               f"({b.type}) are of different types")

    def put_table(self, table, sql):
        """Store a table's (new) definition; reload() turns it into the in-memory schema."""
        self.tree.insert(encode((table.name,)), encode(("table", table.name, sql, table.root)), replace=True)

    def put_index(self, index, sql):
        self.tree.insert(encode((index.name,)), encode(("index", index.name, sql, index.root)), replace=True)

    def drop_entry(self, name):
        self.tree.delete(encode((name,)))

    def drop_stats(self, table):
        """Forget a table's ANALYZE statistics (its columns changed)."""
        for column in range(-1, len(table.columns) + 1):
            self.tree.delete(encode((table.name, "stats", column)))
        table.stats = None

    def rewrite_rows(self, table, change):
        """Replace every version of every row by change(key, row), which may change the row's width."""
        old_width = len(table.columns)
        tree = self.btree(table.root)
        for key, value in list(tree.scan()):
            versions = [[xmin, xmax, tuple(change(key, row))] for xmin, xmax, row in unpack(decode(value), old_width)]
            encoded = encode(pack(versions))
            _check_size(len(key) + len(encoded), f"row of table {table.name}")
            tree.insert(key, encoded, replace=True)

    def drop_meta(self, name):
        if self.meta.pop(name, None) is not None:
            self.tree.delete(encode(("", name)))

    def rename_meta(self, old, new):
        value = self.meta.pop(old, None)
        if value is not None:
            self.tree.delete(encode(("", old)))
            self.set_meta(new, value)

    def referenced(self, fk):
        """Positions, in the parent, of the columns a foreign key references."""
        parent = self.tables[fk.parent]
        if not fk.ref_columns:
            if not parent.primary_key:
                raise SqlError(f"table {parent.name} has no primary key for foreign key {fk.name} to reference")
            return list(parent.primary_key)
        refs = []
        for name in fk.ref_columns:
            pos = parent.column(name)
            if pos is None:
                raise SqlError(f"column {name} referenced by foreign key {fk.name} does not exist in table {parent.name}")
            refs.append(pos)
        return refs

    def referencing(self, name):
        """[(child table, foreign key)] for every foreign key that references table name."""
        return [(t, fk) for t in self.tables.values() for fk in t.foreign_keys if fk.parent == name]

    def nextval(self, table, column):
        """The next value of a column's sequence. As in PostgreSQL it is never handed out twice, even when the
        transaction that took it rolls back (a failed statement's values are reused: its pages roll back)."""
        name = f"seq:{table.name}.{table.columns[column].name}"
        value = self.meta.get(name, 0) + 1
        self.set_meta(name, value)
        return value

    def live_keys(self, table, positions, values, txn):
        """Keys of rows whose columns at positions equal values and that are live for txn: committed (or txn's
        own) and not deleted. A matching row an open transaction is changing raises LockWaitError: PostgreSQL
        would wait for it. Uses the primary key or an index on those columns when there is one."""
        values = tuple(values)

        def matches(row):
            return tuple(row[i] for i in positions) == values

        if positions == table.primary_key:
            key = encode(values)
            versions = self.chain(table, key)
            if versions and self._live(versions, txn, matches):
                yield key
            return
        index = next((i for i in table.indexes if i.columns[:len(positions)] == positions), None)
        if index is not None:
            prefix, size = encode(values), len(index.columns)
            keys = []
            for entry, _ in self.btree(index.root).scan(lo=prefix):
                if not entry.startswith(prefix):
                    break
                key = encode(decode(entry)[size:])
                if key not in keys:
                    keys.append(key)
            candidates = ((k, self.chain(table, k) or []) for k in keys)
        else:
            width = len(table.columns)
            candidates = [(k, unpack(decode(v), width)) for k, v in self.btree(table.root).scan()]
        for key, versions in candidates:
            if self._live(versions, txn, matches):
                yield key

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
        for key, row in list(self.versions(table)):
            self._index_add(index, key, row)
        if index.unique:
            self._check_unique_all(table, index)
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
        self.unsettled.pop(name, None)
        for column in table.columns:
            sequence = f"seq:{name}.{column.name}"
            if self.meta.pop(sequence, None) is not None:
                self.tree.delete(encode(("", sequence)))
        for column in range(-1, len(table.columns)):
            self.tree.delete(encode((name, "stats", column)))
        self._next_rowid.pop(name, None)

    def drop_index(self, name):
        index = self.indexes.pop(name)
        self.tables[index.table].indexes.remove(index)
        self.btree(index.root).drop()
        self.tree.delete(encode((name,)))

    def create_view(self, name, sql):
        self.views[name] = sql
        self._put_entry("view", name, sql, 0)

    def drop_view(self, name):
        del self.views[name]
        self.tree.delete(encode((name,)))

    def dependent_views(self, name):
        """Views whose query reads table or view `name`."""
        from .binder import _references  # noqa: PLC0415 (the binder imports the catalog's module)

        return [v for v, sql in self.views.items() if v != name and _references(parse(sql).query, name)]

    def _check_name_free(self, name):
        if name in self.views:
            raise SqlError(f"view {name} already exists")
        if name in self.tables:
            raise SqlError(f"table {name} already exists")
        if name in self.indexes:
            raise SqlError(f"index {name} already exists")

    def _put_entry(self, kind, name, sql, root):
        self.tree.insert(encode((name,)), encode((kind, name, sql, root)))

    # ---- rows --------------------------------------------------------------------

    def analyze(self, table, snapshot):
        """Collect row and column statistics for the optimizer and store them in the catalog."""
        rows = [row for _, row in self.scan(table, snapshot)]
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
                             encode(("stats", table.name, json.dumps(data, default=_stat_json), column)),
                             replace=True)

    # ---- rows: chains of versions (see mvcc.py) ------------------------------------------------

    def chain(self, table, key):
        """The versions stored under key, oldest first, or None."""
        value = self.btree(table.root).get(key)
        return None if value is None else unpack(decode(value), len(table.columns))

    def _store(self, table, key, versions):
        tree = self.btree(table.root)
        if not versions:
            tree.delete(key)
            return
        value = encode(pack(versions))
        _check_size(len(key) + len(value), f"row of table {table.name}")
        tree.insert(key, value, replace=True)

    def _visible(self, table, versions, snapshot, sx=None, key=None):
        """The version of a row that snapshot sees (raw, untyped), or None. sx: the SERIALIZABLE reader,
        told which transactions wrote versions it cannot see."""
        if len(versions) == 1 and versions[0][0] == FROZEN and versions[0][1] is None:
            return versions[0][2]  # settled: everyone sees it
        for i in range(len(versions) - 1, -1, -1):
            xmin, xmax, row = versions[i]
            if self.mvcc.visible(snapshot, xmin, xmax):
                if sx is not None:
                    self.ssi.saw(sx, versions, i, table.name, key)
                return row
        if sx is not None:
            self.ssi.saw(sx, versions, -1, table.name, key)
        return None

    def scan(self, table, snapshot):
        """Yield (key, row) for every row the snapshot sees, in primary key order."""
        width = len(table.columns)
        sx = self.ssi.reader(snapshot)
        if sx is not None:
            self.ssi.read(sx, table.name)
        for key, value in self.btree(table.root).scan():
            row = self._visible(table, unpack(decode(value), width), snapshot, sx, key)
            if row is not None:
                yield key, self._typed(table, row)

    def versions(self, table):
        """Yield (key, row) for every version of every row, whoever can see it (for building an index)."""
        width = len(table.columns)
        for key, value in self.btree(table.root).scan():
            for _, _, row in unpack(decode(value), width):
                yield key, row

    def scan_range(self, table, index, snapshot, lo=None, hi=None, hi_inclusive=False, index_only=False):
        """Yield (table key, row) for rows whose key in `index` (None = primary key) is in [lo, hi).

        An index entry may belong to any version of its row, so each match is
        checked against the version the snapshot sees (PostgreSQL's recheck).
        index_only builds rows from the entries alone, which is only correct
        while every row of the table is settled; otherwise it checks the table,
        as PostgreSQL does for pages its visibility map does not mark all-visible.
        Columns outside the index come back as NULL.
        """
        width = len(table.columns)
        sx = self.ssi.reader(snapshot)
        if sx is not None:
            # A key range is remembered as read; a secondary index range, more coarsely, as the whole table.
            self.ssi.read(sx, table.name, *((lo, hi, hi_inclusive) if index is None else ()))
        if index is None:
            for key, value in self.btree(table.root).scan(lo, hi, hi_inclusive=hi_inclusive):
                row = self._visible(table, unpack(decode(value), width), snapshot, sx, key)
                if row is not None:
                    yield key, self._typed(table, row)
            return
        tree, size = self.btree(table.root), len(index.columns)
        positions = [*index.columns, *table.primary_key]
        all_visible = not self.unsettled.get(table.name)
        for entry, _ in self.btree(index.root).scan(lo, hi, hi_inclusive=hi_inclusive):
            values = decode(entry)
            key = encode(values[size:])  # an index entry ends with the row's table key
            if index_only and all_visible:
                row = [None] * width
                for position, value in zip(positions, values, strict=False):
                    row[position] = value
                yield key, self._typed(table, tuple(row))
                continue
            stored = tree.get(key)
            row = self._visible(table, unpack(decode(stored), width), snapshot, sx, key) if stored is not None else None
            if row is not None and tuple(row[i] for i in index.columns) == tuple(values[:size]):
                yield key, self._typed(table, row)

    def _typed(self, table, row):
        if table.booleans:
            row = tuple(bool(v) if v is not None and i in table.booleans else v for i, v in enumerate(row))
        if table.scales:
            row = tuple(v.quantize(Decimal(1).scaleb(-table.scales[i])) if v is not None and i in table.scales else v
                        for i, v in enumerate(row))
        return row

    def check_row(self, table, row):
        """Apply NOT NULL and convert values to their column's type (REAL to floats, text to dates and
        numerics, NUMERIC(p, s) rounded to s digits). Returns the row to store."""
        out = []
        for column, value in zip(table.columns, row, strict=True):
            if value is None:
                if column.not_null:
                    raise ConstraintError(f"NULL value in column {column.name} of table {table.name} violates NOT NULL")
            elif column.type == REAL and not isinstance(value, float):
                value = float(value)
            elif column.type not in (INTEGER, TEXT, BOOLEAN) and literal_type(value) != column.type:
                value = cast(value, column.type)  # text to dates, numerics, arrays and JSONB; array elements
            if value is not None and column.scale is not None:
                value = scale_decimal(value, column.scale, column.precision)
            out.append(value)
        return tuple(out)

    def insert_row(self, table, row, txn, rowid_key=None):
        """Add a row produced by check_row as a new version created by txn, maintaining every index.

        Tables without a primary key get a new row id unless rowid_key (an
        existing row's key, when UPDATE rewrites it) is given. Raises
        ConstraintError for a key or unique value another live row holds, and
        LockWaitError when the competing row belongs to an open transaction.
        """
        if table.primary_key:
            key = encode(tuple(row[i] for i in table.primary_key))
        else:
            key = rowid_key if rowid_key is not None else encode((self._take_rowid(table),))
        versions = self.chain(table, key) or []
        if versions and self._live(versions, txn, lambda r: True):
            values = ", ".join(_show(row[i]) for i in table.primary_key)
            raise ConstraintError(f"duplicate key ({values}) violates the primary key of table {table.name}")
        for index in table.indexes:
            self._check_unique(table, index, row, key, txn)
        self._store(table, key, [*versions, [txn.current, None, row]])
        for index in table.indexes:
            self._index_add(index, key, row)
        self._touch(table, key, txn)
        return key

    def lock_row(self, table, key, txn, snapshot):
        """The versions of a row about to be updated or deleted by txn, after the checks PostgreSQL makes.

        Returns (versions, position of the current version), or None if the
        row is gone. Raises LockWaitError if another open transaction already
        changed it, and SerializationError if a transaction that committed after
        a REPEATABLE READ snapshot did.
        """
        versions = self.chain(table, key)
        if not versions:
            return None
        live = [i for i, (xmin, _, _) in enumerate(versions) if xmin in txn.own or self.mvcc.status(xmin) != "aborted"]
        if not live:
            return None
        current = live[-1]
        xmin, xmax, _ = versions[current]
        self._not_held(table, key, xmin, txn)
        if xmax is not None and xmax not in txn.own:
            self._not_held(table, key, xmax, txn)
            if self.mvcc.status(xmax) == "committed":
                if not self.mvcc.sees(snapshot, xmax):
                    raise SerializationError("could not serialize access due to concurrent update: "
                                             f"{self._describe(table, key)} was changed by a transaction that "
                                             "committed after this transaction's snapshot")
                return None  # deleted before our snapshot: we never saw it
        if not self.mvcc.sees(snapshot, xmin):
            raise SerializationError("could not serialize access due to concurrent update: "
                                     f"{self._describe(table, key)} was changed by a transaction that committed "
                                     "after this transaction's snapshot")
        return versions, current

    def delete_version(self, table, key, versions, current, txn):
        """Mark the current version deleted by txn (dropping it outright if txn also created it)."""
        xmin, _, row = versions[current]
        if xmin == txn.current:  # made in this same (sub)transaction: nothing can need it any more
            remaining = versions[:current] + versions[current + 1:]
            self._store(table, key, remaining)
            self._index_clean(table, key, [row], remaining)
        else:
            versions[current][1] = txn.current
            self._store(table, key, versions)
        self._touch(table, key, txn)

    def update_version(self, table, key, versions, current, row, txn):
        """Replace the current version by a new one with the same key: the old one is marked deleted and
        the new one appended, so snapshots that saw the old version keep seeing it. Unique indexes are
        checked by check_unique once the whole statement has written its versions."""
        old = versions[current]
        replaced = old[0] == txn.current  # made in this same (sub)transaction: replace it outright
        if replaced:
            remaining = versions[:current] + versions[current + 1:]
        else:
            old[1] = txn.current
            remaining = versions
        remaining = [*remaining, [txn.current, None, row]]
        self._store(table, key, remaining)
        if replaced:
            self._index_clean(table, key, [old[2]], remaining)
        for index in table.indexes:
            self._index_add(index, key, row)
        self._touch(table, key, txn)

    def prune(self, horizon):
        """Tidy every row a transaction touched since the last prune: drop versions no snapshot can see,
        freeze the rest where possible, and remove index entries no version needs. Returns the
        transaction ids the remaining versions still mention (the commit log must keep those)."""
        mentioned = set()
        for name in list(self.unsettled):
            table = self.tables.get(name)
            keys = self.unsettled.pop(name)
            if table is None:
                continue
            still = set()
            for key in keys:
                versions = self.chain(table, key)
                if versions is None:
                    continue
                kept = prune(versions, self.mvcc, horizon)
                if kept != versions:
                    self._store(table, key, kept)
                    gone = [row for _, _, row in versions if all(row is not k[2] for k in kept)]
                    self._index_clean(table, key, gone, kept)
                if kept and not settled(kept):
                    still.add(key)
                    for xmin, xmax, _ in kept:
                        mentioned.update(x for x in (xmin, xmax) if x)
            if still:
                self.unsettled[name] = still
        self.set_meta("unsettled", 1 if self.unsettled else 0)
        return mentioned

    def vacuum(self, horizon):
        """Prune every row of every table (used at open when rows may still hold unsettled versions)."""
        for table in self.tables.values():
            self.unsettled[table.name] = {key for key, _ in self.btree(table.root).scan()}
        return self.prune(horizon)

    def unsettled_rows(self, limit):
        """[(table, key, versions)] for rows that currently hold more than one version or an unfrozen one."""
        out = []
        for name, keys in self.unsettled.items():
            table = self.tables.get(name)
            if table is None:
                continue
            for key in sorted(keys):
                versions = self.chain(table, key)
                if versions and not settled(versions):
                    out.append((table, key, versions))
                    if len(out) >= limit:
                        return out
        return out

    # ---- helpers --------------------------------------------------------------------------------

    def _touch(self, table, key, txn):
        if not self.unsettled:
            self.set_meta("unsettled", 1)  # logged with the versions, so a crash leaves a vacuum to do
        self.unsettled.setdefault(table.name, set()).add(key)
        txn.touched.setdefault(table.name, set()).add(key)
        self.ssi.wrote(txn, table.name, key)

    def _live(self, versions, txn, matches):
        """Whether some version matching `matches` is live for a writer: created by a transaction that
        committed (or by txn) and not deleted by one. A version an open transaction holds raises
        LockWaitError: PostgreSQL would wait to see whether it commits."""
        for xmin, xmax, row in versions:
            if not matches(row):
                continue
            created = "own" if xmin in txn.own else self.mvcc.status(xmin)
            if created == "aborted":
                continue
            if created == "running":
                raise self._held_error(xmin)
            if xmax is None or (xmax not in txn.own and self.mvcc.status(xmax) == "aborted"):
                return True
            if xmax not in txn.own and self.mvcc.status(xmax) == "running":
                raise self._held_error(xmax)
        return False

    def _not_held(self, table, key, xid, txn):
        if xid not in txn.own and self.mvcc.status(xid) == "running":
            raise self._held_error(xid, self._describe(table, key))

    def _held_error(self, xid, what="a row with that key"):
        holder = self.mvcc.running[xid]
        error = LockWaitError(f"{what} is held by the open transaction {holder.xid} in session {holder.owner}; "
                              "PostgreSQL would make this statement wait until that transaction commits or rolls back",
                              holder.xid, what)
        error.wake = xid  # the (savepoint) id whose end releases the row
        return error

    def _describe(self, table, key):
        values = decode(key)
        if table.primary_key:
            names = [table.columns[i].name for i in table.primary_key]
            return "row (" + ", ".join(f"{n} = {_show(v)}" for n, v in zip(names, values, strict=True)) + ")"
        return f"row {values[0]}"

    def _take_rowid(self, table):
        rowid = self._next_rowid.get(table.name)
        if rowid is None:
            last = self.btree(table.root).last_key()
            rowid = decode(last)[0] + 1 if last is not None else 1
        self._next_rowid[table.name] = rowid + 1
        return rowid

    def _index_add(self, index, key, row):
        entry = encode(tuple(row[i] for i in index.columns)) + key
        _check_size(len(entry), f"entry of index {index.name}")
        tree = self.btree(index.root)
        if tree.get(entry) is None:
            tree.insert(entry, b"")

    def _index_clean(self, table, key, removed, remaining):
        """Delete the index entries of removed versions that no remaining version shares."""
        for index in table.indexes:
            keep = {tuple(r[i] for i in index.columns) for _, _, r in remaining}
            for values in {tuple(r[i] for i in index.columns) for r in removed} - keep:
                self.btree(index.root).delete(encode(values) + key)

    def check_unique(self, table, row, key, txn):
        for index in table.indexes:
            self._check_unique(table, index, row, key, txn)

    def _check_unique(self, table, index, row, key, txn):
        values = tuple(row[i] for i in index.columns)
        if not index.unique or None in values:  # NULLs never collide in a unique index
            return
        prefix, size = encode(values), len(index.columns)
        for existing, _ in self.btree(index.root).scan(lo=prefix):
            if not existing.startswith(prefix):
                break
            other = encode(decode(existing)[size:])
            if other == key:
                continue  # an older version of the same row
            versions = self.chain(table, other) or []
            if self._live(versions, txn, lambda r: tuple(r[i] for i in index.columns) == values):
                shown = ", ".join(_show(v) for v in values)
                raise ConstraintError(f"duplicate value ({shown}) violates unique index {index.name}")

    def _check_unique_all(self, table, index):
        """CREATE UNIQUE INDEX on existing rows: no two live rows may share a value."""
        seen = {}
        for key, value in self.btree(table.root).scan():
            for xmin, xmax, row in unpack(decode(value), len(table.columns)):
                if self.mvcc.status(xmin) == "aborted" or (xmax is not None and self.mvcc.status(xmax) == "committed"):
                    continue
                values = tuple(row[i] for i in index.columns)
                if None in values:
                    continue
                if seen.setdefault(values, key) != key:
                    shown = ", ".join(_show(v) for v in values)
                    raise ConstraintError(f"duplicate value ({shown}) violates unique index {index.name}")

    def set_meta(self, name, value):
        """Store a number in the catalog (the transaction id limit, the unsettled flag, the commit log)."""
        if self.meta.get(name) == value:
            return
        self.meta[name] = value
        self.tree.insert(encode(("", name)), encode(("meta", name, "", value)), replace=True)

    def save_commit_log(self, committed):
        """Keep exactly these committed transaction ids in the stored commit log."""
        stored = set(self.clog)
        for xid in committed - stored:
            self.tree.insert(encode(("", "clog", xid)), encode(("clog", "", "", xid)))
        for xid in stored - committed:
            self.tree.delete(encode(("", "clog", xid)))
        self.clog = set(committed)


def _table_from_ast(stmt, root, sql):
    columns = []
    for c in stmt.columns:
        if any(existing.name == c.name for existing in columns):
            raise SqlError(f"column {c.name} is defined twice in table {stmt.name}")
        if c.type == INTERVAL:
            raise SqlError(f"column {c.name}: Arcanum can't store INTERVAL columns yet; INTERVAL works in expressions")
        default, default_expr = None, None
        if isinstance(c.default, n.Literal):
            default = c.default.value
            if not assignable(literal_type(default), c.type):
                raise SqlError(f"default {_show(default)} does not fit column {c.name} of type {c.type}")
            if c.type == REAL and default is not None:
                default = float(default)
        elif c.default is not None:
            default_expr = c.default  # bound when the table is created, evaluated for each row
        if c.identity and c.default is not None:
            raise SqlError(f"both default and identity specified for column {c.name} of table {stmt.name}")
        precision, scale = c.modifiers if c.modifiers else (None, None)
        columns.append(ColumnSchema(c.name, c.type, c.not_null, default, c.identity, default_expr, precision, scale,
                                    c.default_sql))
    table = TableSchema(stmt.name, columns, [], root, sql)
    names = set()
    for i, check in enumerate(stmt.checks):
        check.name = check.name or (f"{stmt.name}_check" if i == 0 else f"{stmt.name}_check{i}")
        if check.name in names:
            raise SqlError(f"constraint {check.name} is defined twice in table {stmt.name}")
        names.add(check.name)
        table.checks.append(check)
    for fk in stmt.foreign_keys:
        positions = []
        for name in fk.columns:
            pos = table.column(name)
            if pos is None:
                raise SqlError(f"column {name} of a foreign key does not exist in table {stmt.name}")
            positions.append(pos)
        name = fk.name or f"{stmt.name}_{'_'.join(fk.columns)}_fkey"
        if name in names:
            raise SqlError(f"constraint {name} is defined twice in table {stmt.name}")
        names.add(name)
        table.foreign_keys.append(ForeignKeySchema(name, stmt.name, positions, fk.table, list(fk.ref_columns),
                                                   fk.on_delete, fk.on_update))
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


def _stat_json(value):
    """Statistics hold column values; those JSON cannot carry (dates, numerics) travel in the value codec."""
    return {"$": encode((value,)).hex()}


def _stat_value(obj):
    return decode(bytes.fromhex(obj["$"]))[0] if set(obj) == {"$"} else obj
