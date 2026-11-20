"""ALTER TABLE.

A table is stored as its CREATE TABLE statement, so a change edits the schema,
writes it back out as SQL and reloads the catalog. Adding, dropping or retyping
a column also rewrites every version of every row (PostgreSQL rewrites the table
for a volatile default or a type change too). Indexes, CHECK constraints,
sequences and other tables' foreign keys follow renames; views are checked to
still make sense, and the statement fails if one does not.
"""

from ..errors import ConstraintError, SqlError
from ..sql import nodes as n
from ..sql.lexer import QUOTED, WORD, tokenize
from ..sql.parser import parse
from .catalog import ForeignKeySchema, _table_from_ast
from .expr import cast, format_expr, scale_decimal, to_text

_TYPES_WITH_SIZE = {"NUMERIC"}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def literal_sql(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + to_text(value).replace("'", "''") + "'"


def table_sql(table):
    """The CREATE TABLE statement for a table's current schema."""
    parts = []
    for c in table.columns:
        text = f"{quote(c.name)} {c.type}"
        if c.type in _TYPES_WITH_SIZE and c.precision is not None:
            text += f"({c.precision}, {c.scale})"
        if c.identity:
            text += f" GENERATED {c.identity} AS IDENTITY"
        elif c.not_null:
            text += " NOT NULL"
        if c.default_sql is not None:
            text += f" DEFAULT {c.default_sql}"
        elif c.default is not None:
            text += f" DEFAULT {literal_sql(c.default)}"
        elif c.default_expr is not None:
            text += f" DEFAULT {format_expr(c.default_expr)}"
        parts.append(text)
    if table.primary_key:
        parts.append(f"PRIMARY KEY ({', '.join(quote(table.columns[i].name) for i in table.primary_key)})")
    parts.extend(f"CONSTRAINT {quote(check.name)} CHECK ({check.sql})" for check in table.checks)
    for fk in table.foreign_keys:
        refs = f" ({', '.join(quote(c) for c in fk.ref_columns)})" if fk.ref_columns else ""
        parts.append(f"CONSTRAINT {quote(fk.name)} FOREIGN KEY ({', '.join(quote(table.columns[i].name) for i in fk.columns)})"
                     f" REFERENCES {quote(fk.parent)}{refs} ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}")
    return f"CREATE TABLE {quote(table.name)} ({', '.join(parts)})"


def index_sql(index, table):
    columns = ", ".join(quote(table.columns[i].name) for i in index.columns)
    return f"CREATE {'UNIQUE ' if index.unique else ''}INDEX {quote(index.name)} ON {quote(table.name)} ({columns})"


def rename_identifier(sql, old, new):
    """sql with every mention of the name old replaced by new (a CHECK only mentions its own table's columns)."""
    out, last = [], 0
    for tok in tokenize(sql):
        if tok.kind == WORD and tok.value.lower() == old:
            out += [sql[last:tok.pos], quote(new)]
            last = tok.pos + len(tok.value)
        elif tok.kind == QUOTED and tok.value == old and sql.startswith(quote(old), tok.pos):
            out += [sql[last:tok.pos], quote(new)]
            last = tok.pos + len(quote(old))
    return "".join(out) + sql[last:]


def mentions(sql, name):
    return any((t.kind == WORD and t.value.lower() == name) or (t.kind == QUOTED and t.value == name)
               for t in tokenize(sql))


class Alter:
    def __init__(self, db, writer):
        self.db, self.catalog, self.writer = db, db.catalog, writer

    def run(self, stmt):
        if stmt.name not in self.catalog.tables:
            if stmt.if_exists:
                return
            raise SqlError(f"table {stmt.name} does not exist")
        name = stmt.name
        for action in stmt.actions:
            name = getattr(self, "_" + action.kind.lower().replace(" ", "_"))(self.catalog.tables[name], action) or name
        self.check_views(name)

    # ---- helpers ---------------------------------------------------------------------------

    def save(self, table, *others):
        """Write the table (and other changed tables) back as SQL, with its indexes, and reload the catalog."""
        for t in (table, *others):
            self.catalog.put_table(t, table_sql(t))
            for index in t.indexes:
                self.catalog.put_index(index, index_sql(index, t))
        self.catalog.reload()

    def column(self, table, name, if_exists=False):
        pos = table.column(name)
        if pos is None and not if_exists:
            raise SqlError(f"column {name} of table {table.name} does not exist")
        return pos

    def live_rows(self, table):
        return [row for _, row in self.catalog.scan(table, self.db._snapshot)]

    def check_views(self, name):
        """Every view must still bind after the change (PostgreSQL would refuse the change)."""
        from .binder import Binder  # noqa: PLC0415 (the binder imports modules this one is imported from)

        for view, sql in self.catalog.views.items():
            try:
                Binder(self.catalog, sql).bind_query(parse(sql).query)
            except SqlError as e:
                raise SqlError(f"cannot alter table {name}: view {view} depends on it ({e.message})") from None

    def children(self, table):
        """Other tables with a foreign key that references this one."""
        return [child for child, _ in self.catalog.referencing(table.name) if child is not table]

    # ---- columns -----------------------------------------------------------------------------

    def _add_column(self, table, action):
        column_def, checks, foreign_keys = action.definition
        if table.column(column_def.name) is not None:
            if action.if_exists:
                return
            raise SqlError(f"column {column_def.name} of table {table.name} already exists")
        # Build the new column (and its constraints) the way CREATE TABLE does, then move them in.
        part = _table_from_ast(n.CreateTable(table.name, [column_def], [], False, checks, foreign_keys), None, "")
        column, pos = part.columns[0], len(table.columns)
        compiled = dict(self.writer.defaults(part))
        values = {}

        def value(key):
            if key not in values:  # one value per row, shared by all its versions
                if column.identity:
                    v = self.catalog.nextval(part, 0)
                elif 0 in compiled:
                    v = compiled[0]((), ())
                else:
                    v = column.default
                values[key] = self.catalog.check_row(part, [v])[0] if v is not None else None
            return values[key]

        rows = self.live_rows(table)
        if column.not_null and not column.identity and rows and column.default is None and 0 not in compiled:
            raise ConstraintError(f"column {column.name} of table {table.name} contains null values: give it a "
                                  "DEFAULT, or add it without NOT NULL")
        self.catalog.rewrite_rows(table, lambda key, row: (*row, value(key)))
        table.columns.append(column)
        existing = {c.name for c in table.checks} | {fk.name for fk in table.foreign_keys}
        for check in part.checks:
            if check.name in existing:
                check.name = f"{table.name}_{column.name}_check"
            table.checks.append(check)
        for fk in part.foreign_keys:
            fk.columns = [pos]
            self.catalog.validate_foreign_key(table, fk)
            table.foreign_keys.append(fk)
        self.catalog.drop_stats(table)
        self.save(table)
        self.validate(self.catalog.tables[table.name])
        return

    def _drop_column(self, table, action):
        pos = self.column(table, action.column, action.if_exists)
        if pos is None:
            return
        name = action.column
        if pos in table.primary_key:
            raise SqlError(f"cannot drop column {name}: it is part of the primary key of table {table.name}")
        if len(table.columns) == 1:
            raise SqlError(f"cannot drop column {name}: it is the only column of table {table.name}")
        for child in self.children(table):
            for fk in child.foreign_keys:
                if fk.parent == table.name and pos in self.catalog.referenced(fk):
                    raise SqlError(f"cannot drop column {name} because foreign key {fk.name} on table {child.name} "
                                   "references it")
        # As in PostgreSQL, constraints and indexes on the column go with it.
        for index in [i for i in table.indexes if pos in i.columns]:
            self.catalog.drop_index(index.name)
        table.checks = [c for c in table.checks if not mentions(c.sql, name)]
        table.foreign_keys = [fk for fk in table.foreign_keys if pos not in fk.columns]
        for fk in table.foreign_keys:
            fk.columns = [c - (c > pos) for c in fk.columns]
        self.catalog.drop_meta(f"seq:{table.name}.{name}")
        self.catalog.rewrite_rows(table, lambda key, row: row[:pos] + row[pos + 1:])
        table.primary_key = [c - (c > pos) for c in table.primary_key]
        for index in table.indexes:
            index.columns = [c - (c > pos) for c in index.columns]
        del table.columns[pos]
        self.catalog.drop_stats(table)
        self.save(table)
        return

    def _rename_column(self, table, action):
        pos = self.column(table, action.column)
        old, new = action.column, action.new_name
        if table.column(new) is not None:
            raise SqlError(f"column {new} of table {table.name} already exists")
        table.columns[pos].name = new
        for check in table.checks:
            check.sql = rename_identifier(check.sql, old, new)
        changed = []
        for child in self.children(table):
            for fk in child.foreign_keys:
                if fk.parent == table.name and old in fk.ref_columns:
                    fk.ref_columns = [new if c == old else c for c in fk.ref_columns]
                    changed.append(child)
        for fk in table.foreign_keys:  # a table referencing itself
            if fk.parent == table.name:
                fk.ref_columns = [new if c == old else c for c in fk.ref_columns]
        self.catalog.rename_meta(f"seq:{table.name}.{old}", f"seq:{table.name}.{new}")
        self.catalog.drop_stats(table)
        self.save(table, *changed)

    def _rename_table(self, table, action):
        old, new = table.name, action.new_name
        self.catalog._check_name_free(new)
        dependents = self.catalog.dependent_views(old)
        if dependents:
            raise SqlError(f"cannot rename table {old} because view {dependents[0]} depends on it; drop the view first")
        children = self.children(table)
        self.catalog.drop_entry(old)
        self.catalog.drop_stats(table)
        for column in table.columns:
            self.catalog.rename_meta(f"seq:{old}.{column.name}", f"seq:{new}.{column.name}")
        table.name = new
        for fk in [*table.foreign_keys, *(fk for c in children for fk in c.foreign_keys)]:
            if fk.parent == old:
                fk.parent = new
            if fk.table == old:
                fk.table = new
        for keys in (self.catalog.unsettled, self.catalog._next_rowid):
            if old in keys:
                keys[new] = keys.pop(old)
        self.save(table, *children)
        return new

    def _set_default(self, table, action):
        column = table.columns[self.column(table, action.column)]
        part = _table_from_ast(n.CreateTable(table.name, [n.ColumnDef(column.name, column.type, default=action.default,
                                                                      default_sql=action.default_sql)]), None, "")
        self.writer.defaults(part)  # binds an expression default now, so a mistake fails the ALTER
        new = part.columns[0]
        column.default, column.default_expr, column.default_sql = new.default, new.default_expr, new.default_sql
        self.save(table)

    def _drop_default(self, table, action):
        column = table.columns[self.column(table, action.column)]
        column.default = column.default_expr = column.default_sql = None
        self.save(table)

    def _set_not_null(self, table, action):
        pos = self.column(table, action.column)
        if any(row[pos] is None for row in self.live_rows(table)):
            raise ConstraintError(f"column {action.column} of table {table.name} contains null values")
        table.columns[pos].not_null = True
        self.save(table)

    def _drop_not_null(self, table, action):
        pos = self.column(table, action.column)
        if pos in table.primary_key or table.columns[pos].identity:
            raise SqlError(f"column {action.column} is in a primary key or is an identity column; it stays NOT NULL")
        table.columns[pos].not_null = False
        self.save(table)

    def _type(self, table, action):
        pos = self.column(table, action.column)
        column = table.columns[pos]
        if pos in table.primary_key:
            raise SqlError(f"Arcanum can't change the type of primary key column {column.name} yet")
        if any(pos in fk.columns for fk in table.foreign_keys) or any(
                fk.parent == table.name and pos in self.catalog.referenced(fk)
                for child in self.children(table) for fk in child.foreign_keys):
            raise SqlError(f"cannot change the type of column {column.name}: a foreign key uses it")
        if action.type == "INTERVAL":
            raise SqlError("Arcanum can't store INTERVAL columns yet")
        precision, scale = action.modifiers if action.modifiers else (None, None)

        def convert(key, row):
            value = row[pos]
            if value is not None:
                value = cast(value, action.type)
                if scale is not None:
                    value = scale_decimal(value, scale, precision)
            return (*row[:pos], value, *row[pos + 1:])

        self.catalog.rewrite_rows(table, convert)
        column.type, column.precision, column.scale = action.type, precision, scale
        if column.default is not None:
            column.default = cast(column.default, action.type)
        rebuilt = [i for i in table.indexes if pos in i.columns]
        self.catalog.drop_stats(table)
        self.save(table)
        for index in rebuilt:  # the entries were encoded as the old type
            sql = index_sql(index, self.catalog.tables[table.name])
            self.catalog.drop_index(index.name)
            self.catalog.create_index(parse(sql), sql)
        self.validate(self.catalog.tables[table.name])

    # ---- constraints -------------------------------------------------------------------------

    def _add_constraint(self, table, action):
        constraint = action.definition
        names = {c.name for c in table.checks} | {fk.name for fk in table.foreign_keys} | {i.name for i in table.indexes}
        if isinstance(constraint, tuple):  # ("UNIQUE", name, columns)
            _, name, columns = constraint
            for c in columns:
                self.column(table, c)
            name = name or f"{table.name}_{'_'.join(columns)}_key"
            sql = f"CREATE UNIQUE INDEX {quote(name)} ON {quote(table.name)} ({', '.join(quote(c) for c in columns)})"
            self.catalog.create_index(parse(sql), sql)
            return
        if isinstance(constraint, n.Check):
            constraint.name = constraint.name or f"{table.name}_check"
            if constraint.name in names:
                raise SqlError(f"constraint {constraint.name} of table {table.name} already exists")
            table.checks.append(constraint)
            self.save(table)
            table = self.catalog.tables[table.name]
            for row in self.live_rows(table):  # existing rows must pass, as in PostgreSQL
                self.writer.check(table, row)
            return
        positions = [self.column(table, c) for c in constraint.columns]
        fk = ForeignKeySchema(constraint.name or f"{table.name}_{'_'.join(constraint.columns)}_fkey", table.name,
                              positions, constraint.table, list(constraint.ref_columns), constraint.on_delete,
                              constraint.on_update)
        if fk.name in names:
            raise SqlError(f"constraint {fk.name} of table {table.name} already exists")
        self.catalog.validate_foreign_key(table, fk)
        table.foreign_keys.append(fk)
        self.save(table)
        table = self.catalog.tables[table.name]
        self.writer.check_parents(table, self.live_rows(table))
        return

    def _drop_constraint(self, table, action):
        name = action.new_name
        if name == f"{table.name}_pkey":
            raise SqlError("Arcanum can't drop a primary key yet")
        if any(c.name == name for c in table.checks):
            table.checks = [c for c in table.checks if c.name != name]
        elif any(fk.name == name for fk in table.foreign_keys):
            table.foreign_keys = [fk for fk in table.foreign_keys if fk.name != name]
        elif any(i.name == name and i.unique for i in table.indexes):
            for child in self.children(table):
                for fk in child.foreign_keys:
                    index = next(i for i in table.indexes if i.name == name)
                    if fk.parent == table.name and self.catalog.referenced(fk) == index.columns:
                        raise SqlError(f"cannot drop constraint {name} because foreign key {fk.name} on table "
                                       f"{child.name} needs it")
            self.catalog.drop_index(name)
            return
        elif action.if_exists:
            return
        else:
            raise SqlError(f"constraint {name} of table {table.name} does not exist")
        self.save(table)
        return

    def validate(self, table):
        """The table's CHECKs and defaults still bind, and existing rows still pass the CHECKs."""
        self.writer._checks.pop(table.name, None)
        self.writer.validate(table)
        if table.checks:
            for row in self.live_rows(table):
                self.writer.check(table, row)
