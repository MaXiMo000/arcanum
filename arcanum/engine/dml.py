"""INSERT, UPDATE and DELETE with their constraints, the way PostgreSQL runs them.

- Identity columns (SERIAL, GENERATED ... AS IDENTITY) take the next value of
  their sequence when a row does not give one.
- CHECK constraints are evaluated on every row written; NULL passes, as in SQL.
- Foreign keys are checked once the statement has written its rows, so rows
  that reference each other within one statement are fine. A referenced row
  that is deleted, or whose key changes, applies the constraint's action to the
  rows that reference it: NO ACTION and RESTRICT refuse, CASCADE deletes them,
  SET NULL clears their key. A row another open transaction is changing makes
  the statement wait, as PostgreSQL's row locks do.
- INSERT ... ON CONFLICT finds a live row with the same key on the conflict
  target, then skips the new row (DO NOTHING) or updates the existing one with
  the proposed row available as EXCLUDED (DO UPDATE).
- RETURNING produces rows from what was written: the new rows for INSERT and
  UPDATE, the deleted rows for DELETE.

Each constraint check, cascade, sequence step and conflict is reported as an
engine event, so the event log shows the work a constraint costs.
"""

from collections import Counter

from ..errors import ConstraintError, SqlError
from ..sql import nodes as n
from ..storage import encode
from .binder import Entry, Level, _default_name
from .executor import run
from .expr import Col, SubqueryExpr, assignable, contains, to_text

MAX_CASCADE_DEPTH = 64


def _show(values):
    return ", ".join("NULL" if v is None else to_text(v) for v in values)


class Writer:
    def __init__(self, db, binder):
        self.db, self.catalog, self.binder = db, db.catalog, binder
        self.txn = db._txn
        self.ctx = db._context()
        self.emit = db.events.emit
        self._checks = {}  # table name -> [(Check, compiled condition)]
        self.verified = Counter()  # (kind, constraint, table) -> rows checked
        self.sequences = {}  # (table, column) -> [first, last] values taken
        self.conflicts = Counter()  # (table, action, constraint) -> rows

    # ---- statements ----------------------------------------------------------------------

    def insert(self, stmt):
        from .database import Result  # noqa: PLC0415 (database imports this module)

        table, positions, source = self.binder.bind_insert(stmt)
        for pos in positions:
            if table.columns[pos].identity == "ALWAYS":
                raise ConstraintError(f"cannot insert a value into column {table.columns[pos].name}: it is an "
                                      "identity column defined as GENERATED ALWAYS")
        returning = self.returning(table, stmt.returning)
        conflict = self.bind_conflict(table, stmt.on_conflict)
        rows = []
        for values in run(self.db._plan(source), self.ctx):
            self.ctx.hold()
            rows.append(values)  # read every source row first: INSERT ... SELECT never sees its own rows
        generated = [i for i, c in enumerate(table.columns) if c.identity and i not in positions]
        computed = [(i, f) for i, f in self.defaults(table) if i not in positions]
        written, out, touched = [], [], set()
        for values in rows:
            row = [c.default for c in table.columns]
            for pos, value in zip(positions, values, strict=True):
                row[pos] = value
            for pos in generated:
                row[pos] = self.next_value(table, pos)
            for pos, f in computed:
                row[pos] = f((), ())
            row = self.catalog.check_row(table, row)
            self.check(table, row)
            hit = self.conflicting(table, row, conflict) if conflict else None
            if hit is not None:
                key, arbiter = hit
                if conflict["assignments"] is None:
                    self.conflicts[(table.name, "nothing", arbiter)] += 1
                    continue
                if key in touched:
                    raise ConstraintError("ON CONFLICT DO UPDATE command cannot affect row a second time: two "
                                          "proposed rows have the same key")
                new = self.update_on_conflict(table, key, row, conflict, arbiter)
                if new is not None:
                    touched.add(key)
                    written.append(new)
                    out.append(new)
                continue
            touched.add(self.catalog.insert_row(table, row, self.txn))
            written.append(row)
            out.append(row)
        self.check_parents(table, written)
        self.report()
        return self.result(Result, returning, out, f"INSERT {len(out)}")

    def update(self, stmt):
        from .database import Result  # noqa: PLC0415

        table, where, assignments = self.binder.bind_update(stmt)
        for pos, _ in assignments:
            if table.columns[pos].identity == "ALWAYS":
                raise ConstraintError(f"column {table.columns[pos].name} can only be updated to DEFAULT: it is an "
                                      "identity column defined as GENERATED ALWAYS")
        returning = self.returning(table, stmt.returning)
        targets = self.db._locked_rows(self.ctx, table, where)
        compiled = [(pos, self.ctx.compile(e)) for pos, e in assignments]
        changes = []
        for key, row, versions, current in targets:
            new = list(row)
            for pos, f in compiled:
                new[pos] = f(row, ())
            new = self.catalog.check_row(table, new)
            self.check(table, new)
            changes.append((key, row, versions, current, new))
        self.write_updates(table, changes)
        self.report()
        return self.result(Result, returning, [c[4] for c in changes], f"UPDATE {len(changes)}")

    def delete(self, stmt):
        from .database import Result  # noqa: PLC0415

        table, where = self.binder.bind_delete(stmt)
        returning = self.returning(table, stmt.returning)
        targets = self.db._locked_rows(self.ctx, table, where)
        for key, _, versions, current in targets:
            self.catalog.delete_version(table, key, versions, current, self.txn)
        self.removed(table, [row for _, row, _, _ in targets], deleting=True)
        self.report()
        return self.result(Result, returning, [row for _, row, _, _ in targets], f"DELETE {len(targets)}")

    def write_updates(self, table, changes):
        """Write [(key, old row, versions, position, new row)] like UPDATE does, then apply the constraints."""
        stayed = [c for c in changes if not table.primary_key
                  or encode(tuple(c[4][i] for i in table.primary_key)) == c[0]]
        moved = [c for c in changes if c not in stayed]
        # Every old version is marked deleted before uniqueness is checked, so swapping keys or unique
        # values between rows (UPDATE t SET id = id + 1) is not mistaken for a duplicate.
        for key, _, versions, current, new in stayed:
            self.catalog.update_version(table, key, versions, current, new, self.txn)
        for key, _, versions, current, _ in moved:
            self.catalog.delete_version(table, key, versions, current, self.txn)
        for *_, new in moved:
            self.catalog.insert_row(table, new, self.txn)
        for key, *_, new in stayed:
            self.catalog.check_unique(table, new, key, self.txn)
        self.check_parents(table, [new for _, old, _, _, new in changes if self._fk_changed(table, old, new)])
        self.removed(table, [old for _, old, _, _, new in changes if old != new], deleting=False,
                     replaced=[new for _, old, _, _, new in changes if old != new])

    # ---- RETURNING and ON CONFLICT -----------------------------------------------------------

    def returning(self, table, items):
        if items is None:
            return None
        level = self.binder.table_level(table)
        out = []
        for item in items:
            if isinstance(item.expr, n.Star):
                out.extend((name, self.ctx.compile(e)) for e, name in self.binder.expand_star(level, item.expr))
            else:
                e = self.binder.plain_expr(level, item.expr, "RETURNING")
                out.append((item.alias or _default_name(item.expr), self.ctx.compile(e)))
        return out

    def result(self, result_type, returning, rows, status):
        count = len(rows)
        if returning is None:
            return result_type(rowcount=count, status=status)
        out = [tuple(f(tuple(row), ()) for _, f in returning) for row in rows]
        return result_type([name for name, _ in returning], out, rowcount=count, status=status, row_total=len(out))

    def bind_conflict(self, table, clause):
        if clause is None:
            return None
        if clause.columns is None:
            arbiters = self._unique_sets(table)
        else:
            positions = sorted(self.binder.column_of(table, c) for c in clause.columns)
            arbiters = [a for a in self._unique_sets(table) if sorted(a[1]) == positions]
            if not arbiters:
                raise SqlError("there is no unique or exclusion constraint matching the ON CONFLICT specification: "
                               f"({', '.join(clause.columns)}) must be the primary key or have a unique index")
        bound = {"arbiters": arbiters, "assignments": None, "where": None}
        if clause.assignments is not None:
            # The existing row's columns come first, then the proposed row's as EXCLUDED.
            width = len(table.columns)
            level = self.binder.table_level(table)
            level.entries += [Entry("excluded", c.name, c.type, Col(width + i, c.type, c.name), star=False,
                                    unqualified=False) for i, c in enumerate(table.columns)]
            assignments = []
            for name, node in clause.assignments:
                pos = self.binder.column_of(table, name)
                value = self.binder.plain_expr(level, node, "ON CONFLICT DO UPDATE")
                if not assignable(value.type, table.columns[pos].type):
                    raise self.binder.error(f"column {name} is {table.columns[pos].type} but the value is "
                                            f"{value.type}", node)
                assignments.append((pos, self.ctx.compile(value)))
            bound["assignments"] = assignments
            if clause.where is not None:
                bound["where"] = self.ctx.compile(self.binder.condition(level, clause.where, "WHERE"))
        return bound

    def _unique_sets(self, table):
        """(constraint name, column positions) of the primary key and every unique index."""
        sets = [(f"{table.name}_pkey", list(table.primary_key))] if table.primary_key else []
        return sets + [(i.name, list(i.columns)) for i in table.indexes if i.unique]

    def conflicting(self, table, row, conflict):
        """(key, constraint) of a live row the proposed row collides with on a conflict target, or None."""
        for name, positions in conflict["arbiters"]:
            values = tuple(row[i] for i in positions)
            if None in values:
                continue  # NULLs never collide
            key = next(self.catalog.live_keys(table, positions, values, self.txn), None)
            if key is not None:
                return key, name
        return None

    def update_on_conflict(self, table, key, proposed, conflict, arbiter):
        hit = self.catalog.lock_row(table, key, self.txn, self.ctx.snapshot)
        if hit is None:
            return None
        versions, current = hit
        old = self.catalog._typed(table, versions[current][2])
        both = (*old, *proposed)
        if conflict["where"] is not None and conflict["where"](both, ()) is not True:
            self.conflicts[(table.name, "skipped", arbiter)] += 1
            return None
        new = list(old)
        for pos, f in conflict["assignments"]:
            new[pos] = f(both, ())
        new = self.catalog.check_row(table, new)
        self.check(table, new)
        self.conflicts[(table.name, "update", arbiter)] += 1
        self.write_updates(table, [(key, old, versions, current, new)])
        return new

    # ---- identity, CHECK -------------------------------------------------------------------

    def next_value(self, table, pos):
        value = self.catalog.nextval(table, pos)
        self.sequences.setdefault((table.name, table.columns[pos].name), [value, value])[1] = value
        return value

    def validate(self, table):
        """Bind a new table's CHECK constraints and default expressions, so mistakes fail its CREATE TABLE."""
        self.checks(table)
        self.defaults(table)

    def defaults(self, table):
        """[(position, compiled default)] for columns whose default is an expression, such as now()."""
        out = []
        for i, column in enumerate(table.columns):
            if column.default_expr is None:
                continue
            e = self.binder.plain_expr(Level(), column.default_expr, "DEFAULT")
            if contains(e, SubqueryExpr):
                raise self.binder.error(f"the default of column {column.name} cannot use a subquery")
            if not assignable(e.type, column.type):
                raise self.binder.error(f"the default of column {column.name} is {e.type}, not {column.type}")
            out.append((i, self.ctx.compile(e)))
        return out

    def checks(self, table):
        compiled = self._checks.get(table.name)
        if compiled is None:
            level = self.binder.table_level(table)
            compiled = []
            for check in table.checks:
                e = self.binder.condition(level, check.expr, "CHECK")
                if contains(e, SubqueryExpr):
                    raise self.binder.error(f"check constraint {check.name} cannot use a subquery", check.expr)
                compiled.append((check, self.ctx.compile(e)))
            self._checks[table.name] = compiled
        return compiled

    def check(self, table, row):
        typed = self.catalog._typed(table, tuple(row))
        for check, condition in self.checks(table):
            if condition(typed, ()) is False:
                raise ConstraintError(f"new row for table {table.name} violates check constraint {check.name}: "
                                      f"CHECK ({check.sql}) is false for ({_show(typed)})")
            self.verified[("check", check.name, table.name)] += 1

    # ---- foreign keys --------------------------------------------------------------------

    def check_parents(self, table, rows):
        """Every non-NULL foreign key of the written rows must match a live row of the referenced table."""
        for fk in table.foreign_keys:
            parent = self.catalog.tables[fk.parent]
            refs = self.catalog.referenced(fk)
            for row in rows:
                values = tuple(row[i] for i in fk.columns)
                if None in values:
                    continue  # MATCH SIMPLE: a NULL anywhere in the key means nothing is referenced
                if next(self.catalog.live_keys(parent, refs, values, self.txn), None) is None:
                    names = ", ".join(table.columns[i].name for i in fk.columns)
                    raise ConstraintError(f"insert or update on table {table.name} violates foreign key constraint "
                                          f"{fk.name}: key ({names})=({_show(values)}) is not present in table "
                                          f"{parent.name}")
                self.verified[("foreign key", fk.name, table.name)] += 1

    def _fk_changed(self, table, old, new):
        return any(any(old[i] != new[i] for i in fk.columns) for fk in table.foreign_keys)

    def removed(self, table, rows, deleting, replaced=None, depth=0):
        """Rows of table were deleted (or, if not deleting, updated into `replaced`): apply the action of every
        foreign key that references them."""
        if depth > MAX_CASCADE_DEPTH:
            raise ConstraintError(f"foreign key cascades go deeper than {MAX_CASCADE_DEPTH} tables")
        for child, fk in self.catalog.referencing(table.name):
            refs = self.catalog.referenced(fk)
            action = fk.on_delete if deleting else fk.on_update
            gone = []
            for i, row in enumerate(rows):
                values = tuple(row[c] for c in refs)
                if None in values or (replaced and tuple(replaced[i][c] for c in refs) == values):
                    continue  # the key did not change
                if next(self.catalog.live_keys(table, refs, values, self.txn), None) is not None:
                    continue  # another live row still has this key
                gone.append(values)
            for values in gone:
                keys = list(self.catalog.live_keys(child, fk.columns, values, self.txn))
                if not keys:
                    continue
                if action in ("NO ACTION", "RESTRICT"):
                    names = ", ".join(table.columns[c].name for c in refs)
                    raise ConstraintError(f"{'delete' if deleting else 'update'} on table {table.name} violates "
                                          f"foreign key constraint {fk.name} on table {child.name}: key ({names})="
                                          f"({_show(values)}) is still referenced from table {child.name}")
                self.cascade(child, fk, keys, action, depth)
            if gone:
                self.verified[("referenced", fk.name, child.name)] += len(gone)

    def cascade(self, child, fk, keys, action, depth):
        old_rows, changes = [], []
        for key in keys:
            hit = self.catalog.lock_row(child, key, self.txn, self.ctx.snapshot)
            if hit is None:
                continue
            versions, current = hit
            old = self.catalog._typed(child, versions[current][2])
            if action == "CASCADE":
                self.catalog.delete_version(child, key, versions, current, self.txn)
                old_rows.append(old)
            else:  # SET NULL
                new = list(old)
                for i in fk.columns:
                    new[i] = None
                new = self.catalog.check_row(child, new)
                self.check(child, new)
                changes.append((key, old, versions, current, new))
        self.emit("cascade", constraint=fk.name, table=child.name, action=action, rows=len(old_rows) + len(changes))
        if old_rows:
            self.removed(child, old_rows, deleting=True, depth=depth + 1)
        for key, _, versions, current, new in changes:
            self.catalog.update_version(child, key, versions, current, new, self.txn)
        if changes:
            self.removed(child, [c[1] for c in changes], deleting=False, replaced=[c[4] for c in changes],
                         depth=depth + 1)

    def report(self):
        for (kind, name, table), rows in self.verified.items():
            self.emit("constraint", constraint_kind=kind, name=name, table=table, rows=rows)
        self.verified.clear()
        for (table, column), (first, last) in self.sequences.items():
            self.emit("sequence", table=table, column=column, first=first, last=last)
        self.sequences.clear()
        for (table, action, constraint), rows in self.conflicts.items():
            self.emit("conflict", table=table, action=action, constraint=constraint, rows=rows)
        self.conflicts.clear()
