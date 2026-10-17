"""The public entry point: open a database file and run SQL against it."""

from dataclasses import dataclass, field

from ..errors import SqlError
from ..sql import nodes as n
from ..sql.parser import parse_statements
from ..storage import Pager
from .binder import Binder
from .catalog import Catalog
from .executor import SORT_MEMORY_ROWS, Context, index_scan_pairs, run
from .optimizer import Optimizer
from .plan import IndexScan, TableScan, explain, plan_tree

_WRITES = (n.Insert, n.Update, n.Delete, n.CreateTable, n.DropTable, n.CreateIndex, n.DropIndex, n.Analyze)


@dataclass
class Result:
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    rowcount: int = -1  # rows changed by INSERT, UPDATE or DELETE
    status: str = ""  # e.g. "INSERT 3", "CREATE TABLE"
    plan: dict | None = None  # the query plan as a tree (see plan.plan_tree), when traced


class Database:
    """A database stored in one file.

    Every statement is atomic: if it fails, none of its changes remain.
    BEGIN ... COMMIT groups statements; ROLLBACK undoes the group. A commit is
    durable once it returns: it is in the write-ahead log, and opening the
    database after a crash replays every committed transaction and drops the rest.

    max_keys caps the keys per B+tree node, so trees split after a few rows;
    useful for demos and tests. optimize=False runs plans exactly as the binder
    builds them, to compare against the optimizer. sort_memory_rows is how many
    rows a sort keeps in memory before spilling sorted runs to disk. fs,
    checkpoint_frames and max_pages are passed to the Pager.
    """

    def __init__(self, path, pool_size=256, max_keys=None, events=None, fs=None, checkpoint_frames=1000,
                 optimize=True, sort_memory_rows=SORT_MEMORY_ROWS, max_pages=None):
        self.optimize = optimize
        self.sort_memory_rows = sort_memory_rows
        self.pager = Pager(path, pool_size, events, fs, checkpoint_frames, max_pages)
        self.events = self.pager.events
        self.catalog = Catalog(self.pager, max_keys)
        self.pager.commit()
        self.in_transaction = False

    @property
    def recovered_frames(self):
        """How many committed log frames were replayed when this database was opened."""
        return self.pager.recovered_frames

    def execute(self, sql, trace=False):
        """Run one statement and return its Result.

        trace=True also returns the plan of a query or EXPLAIN as a tree, with
        each operator's actual rows and time.
        """
        statements = parse_statements(sql)
        if len(statements) != 1:
            raise SqlError(f"execute() runs exactly one statement, got {len(statements)}; use execute_script()")
        statement, text = statements[0]
        return self._execute(statement, text, sql, trace)

    def execute_script(self, sql, trace=False):
        """Run several semicolon-separated statements; returns one Result per statement."""
        return [self._execute(statement, text, sql, trace) for statement, text in parse_statements(sql)]

    def close(self):
        if self.in_transaction:
            self.pager.rollback()
            self.in_transaction = False
        self.pager.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- statement dispatch ---------------------------------------------------------

    def _execute(self, statement, text, sql, trace=False):
        if isinstance(statement, n.Begin):
            if self.in_transaction:
                raise SqlError("a transaction is already in progress")
            self.pager.savepoint()
            self.in_transaction = True
            return Result(status="BEGIN")
        if isinstance(statement, n.Commit):
            self._require_transaction()
            self.pager.release()
            self.in_transaction = False
            self.pager.commit()
            return Result(status="COMMIT")
        if isinstance(statement, n.Rollback):
            self._require_transaction()
            self.pager.rollback()
            self.catalog.reload()
            self.in_transaction = False
            return Result(status="ROLLBACK")

        self.pager.savepoint()
        try:
            result = self._run(statement, text, sql, trace)
        except BaseException:
            self.pager.rollback()
            self.catalog.reload()
            raise
        self.pager.release()
        if not self.in_transaction and isinstance(statement, _WRITES):
            self.pager.commit()
        return result

    def _require_transaction(self):
        if not self.in_transaction:
            raise SqlError("there is no transaction in progress")

    def _run(self, statement, text, sql, trace=False):
        binder = Binder(self.catalog, sql)
        if isinstance(statement, n.Query):
            plan = self._plan(binder.bind_query(statement))
            ctx = self._context(analyze=trace)
            rows = list(run(plan, ctx))
            return Result([c.name for c in plan.columns], rows, status=f"SELECT {len(rows)}",
                          plan=plan_tree(plan, ctx.stats) if trace else None)
        if isinstance(statement, n.Explain):
            if not isinstance(statement.statement, n.Query):
                raise SqlError("Arcanum doesn't support EXPLAIN for statements other than queries yet")
            plan = self._plan(binder.bind_query(statement.statement))
            stats = None
            if statement.analyze:
                ctx = self._context(analyze=True)
                for _ in run(plan, ctx):
                    pass
                stats = ctx.stats
            return Result(["plan"], [(line,) for line in explain(plan, stats)], status="EXPLAIN",
                          plan=plan_tree(plan, stats) if trace else None)
        if isinstance(statement, n.Insert):
            return self._insert(binder, statement)
        if isinstance(statement, n.Update):
            return self._update(binder, statement)
        if isinstance(statement, n.Delete):
            return self._delete(binder, statement)
        if isinstance(statement, n.CreateTable):
            if statement.name in self.catalog.tables and statement.if_not_exists:
                return Result(status="CREATE TABLE")
            self.catalog.create_table(statement, text)
            return Result(status="CREATE TABLE")
        if isinstance(statement, n.CreateIndex):
            if statement.name in self.catalog.indexes and statement.if_not_exists:
                return Result(status="CREATE INDEX")
            self.catalog.create_index(statement, text)
            return Result(status="CREATE INDEX")
        if isinstance(statement, n.DropTable):
            if statement.name not in self.catalog.tables:
                if statement.if_exists:
                    return Result(status="DROP TABLE")
                raise SqlError(f"table {statement.name} does not exist")
            self.catalog.drop_table(statement.name)
            return Result(status="DROP TABLE")
        if isinstance(statement, n.DropIndex):
            if statement.name not in self.catalog.indexes:
                if statement.if_exists:
                    return Result(status="DROP INDEX")
                raise SqlError(f"index {statement.name} does not exist")
            self.catalog.drop_index(statement.name)
            return Result(status="DROP INDEX")
        if isinstance(statement, n.Analyze):
            if statement.table is not None:
                tables = [binder.table(statement.table)]
            else:
                tables = list(self.catalog.tables.values())
            for table in tables:
                self.catalog.analyze(table)
            return Result(status="ANALYZE")
        raise SqlError(f"Arcanum doesn't support {type(statement).__name__} yet")

    def _insert(self, binder, statement):
        table, positions, source = binder.bind_insert(statement)
        source = self._plan(source)
        # Read every source row first, so INSERT ... SELECT from the same table never sees its own inserts.
        rows = list(run(source, self._context()))
        defaults = [c.default for c in table.columns]
        for values in rows:
            row = list(defaults)
            for pos, value in zip(positions, values):
                row[pos] = value
            self.catalog.insert_row(table, self.catalog.check_row(table, row))
        return Result(rowcount=len(rows), status=f"INSERT {len(rows)}")

    def _context(self, analyze=False):
        return Context(self.catalog, analyze, self.sort_memory_rows)

    def _plan(self, plan):
        return Optimizer().optimize(plan) if self.optimize else plan

    def _matching_rows(self, ctx, table, where):
        """(key, row) of every row the WHERE clause selects, through an index when one helps."""
        scan, conditions = TableScan(table, []), [where] if where is not None else []
        if self.optimize and where is not None:
            optimizer = Optimizer()
            optimizer.optimize_expr(where)
            scan, conditions = optimizer.access_path(scan, _conjuncts(where))
        pairs = index_scan_pairs(scan, ctx) if isinstance(scan, IndexScan) else self.catalog.scan(table)
        checks = [ctx.compile(c) for c in conditions]
        return [(key, row) for key, row in pairs if all(check(row, ()) is True for check in checks)]

    def _update(self, binder, statement):
        table, where, assignments = binder.bind_update(statement)
        ctx = self._context()
        targets = self._matching_rows(ctx, table, where)
        compiled = [(pos, ctx.compile(e)) for pos, e in assignments]
        updated = []
        for key, row in targets:
            new = list(row)
            for pos, f in compiled:
                new[pos] = f(row, ())
            updated.append(self.catalog.check_row(table, new))
        # Remove every old version before writing new ones, so swapping key values
        # between rows (UPDATE t SET id = id + 1) is not mistaken for a duplicate.
        for key, row in targets:
            self.catalog.delete_row(table, key, row)
        for (key, _), row in zip(targets, updated):
            self.catalog.insert_row(table, row, None if table.primary_key else key)
        return Result(rowcount=len(targets), status=f"UPDATE {len(targets)}")

    def _delete(self, binder, statement):
        table, where = binder.bind_delete(statement)
        targets = self._matching_rows(self._context(), table, where)
        for key, row in targets:
            self.catalog.delete_row(table, key, row)
        return Result(rowcount=len(targets), status=f"DELETE {len(targets)}")


def _conjuncts(e):
    if getattr(e, "op", None) == "AND":
        return _conjuncts(e.args[0]) + _conjuncts(e.args[1])
    return [e]
