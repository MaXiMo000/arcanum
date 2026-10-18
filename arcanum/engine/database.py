"""The public entry point: open a database file and run SQL against it."""

from dataclasses import dataclass, field

from ..errors import ArcanumError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse_statements
from ..storage import Pager
from .advisor import advise
from .binder import Binder
from .catalog import Catalog
from .executor import Context, QueryLimits, index_scan_pairs, run
from .expr import conjuncts
from .optimizer import Optimizer, mark_index_only
from .plan import IndexScan, TableScan, explain, plan_tree

_WRITES = (n.Insert, n.Update, n.Delete, n.CreateTable, n.DropTable, n.CreateIndex, n.DropIndex, n.Analyze)


@dataclass
class Result:
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    rowcount: int = -1  # rows changed by INSERT, UPDATE or DELETE
    status: str = ""  # e.g. "INSERT 3", "CREATE TABLE"
    plan: dict | None = None  # the query plan as a tree (see plan.plan_tree), when traced
    row_total: int = 0  # rows the query produced; more than len(rows) when max_rows cut them off
    advice: list = field(default_factory=list)  # the advisor's hints (see advisor.py), when traced
    statement: str = ""  # the statement's own text, when traced


class Database:
    """A database stored in one file.

    Every statement is atomic: if it fails, none of its changes remain.
    BEGIN ... COMMIT groups statements; ROLLBACK undoes the group. A commit is
    durable once it returns: it is in the write-ahead log, and opening the
    database after a crash replays every committed transaction and drops the rest.

    max_keys caps the keys per B+tree node, so trees split after a few rows;
    useful for demos and tests. optimize=False runs plans exactly as the binder
    builds them, to compare against the optimizer. limits (a QueryLimits) bounds
    what one statement may use: rows a sort keeps in memory before spilling,
    rows held in memory, temporary sort space and recursive CTE size. fs,
    checkpoint_frames and max_pages are passed to the Pager.
    """

    def __init__(self, path, pool_size=256, max_keys=None, events=None, fs=None, checkpoint_frames=1000,
                 optimize=True, limits=None, max_pages=None):
        self.optimize = optimize
        self.limits = limits or QueryLimits()
        self.pager = Pager(path, pool_size, events, fs, checkpoint_frames, max_pages)
        self.events = self.pager.events
        self.catalog = Catalog(self.pager, max_keys)
        self.pager.commit()
        self.in_transaction = False

    @property
    def recovered_frames(self):
        """How many committed log frames were replayed when this database was opened."""
        return self.pager.recovered_frames

    def execute(self, sql, trace=False, max_rows=None):
        """Run one statement and return its Result.

        trace=True also returns the plan of a query or EXPLAIN as a tree, with
        each operator's actual rows and time. max_rows keeps only the first rows
        of a query's result (the rest are counted in row_total, not stored).
        """
        statements = parse_statements(sql)
        if len(statements) != 1:
            raise SqlError(f"execute() runs exactly one statement, got {len(statements)}; use execute_script()")
        statement, text = statements[0]
        return self._execute(statement, text, sql, trace, max_rows)

    def execute_script(self, sql, trace=False, max_rows=None):
        """Run several semicolon-separated statements; returns one Result per statement.

        If one fails, the error carries `completed` (how many statements before
        it ran, and stay run) and `statement_start` (its offset in sql).
        """
        results, cursor = [], 0
        for statement, text in parse_statements(sql):
            start = sql.find(text, cursor)
            cursor = start + len(text)
            try:
                results.append(self._execute(statement, text, sql, trace, max_rows))
            except ArcanumError as e:
                e.completed, e.statement_start = len(results), start
                raise
        return results

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

    def _execute(self, statement, text, sql, trace=False, max_rows=None):
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
            result = self._run(statement, text, sql, trace, max_rows)
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

    def _run(self, statement, text, sql, trace=False, max_rows=None):
        binder = Binder(self.catalog, sql)
        if isinstance(statement, n.Query):
            plan = self._plan(binder.bind_query(statement))
            ctx = self._context(analyze=trace)
            rows, total = [], 0
            for row in run(plan, ctx):
                total += 1
                if max_rows is None or total <= max_rows:
                    rows.append(row)
            result = Result([c.name for c in plan.columns], rows, status=f"SELECT {total}", row_total=total)
            return self._traced(result, statement, text, plan, ctx.stats) if trace else result
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
            result = Result(["plan"], [(line,) for line in explain(plan, stats)], status="EXPLAIN")
            return self._traced(result, statement, text, plan, stats) if trace else result
        if isinstance(statement, n.Insert):
            return self._insert(binder, statement)
        if isinstance(statement, (n.Update, n.Delete)):
            result = self._update(binder, statement) if isinstance(statement, n.Update) else self._delete(binder, statement)
            return self._traced(result, statement, text) if trace else result
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
        ctx, rows = self._context(), []
        for values in run(source, ctx):
            ctx.hold()
            rows.append(values)
        defaults = [c.default for c in table.columns]
        for values in rows:
            row = list(defaults)
            for pos, value in zip(positions, values, strict=True):
                row[pos] = value
            self.catalog.insert_row(table, self.catalog.check_row(table, row))
        return Result(rowcount=len(rows), status=f"INSERT {len(rows)}")

    def _traced(self, result, statement, text, plan=None, stats=None):
        """Attach the plan tree (with actual rows and time, if measured) and the advisor's hints."""
        ids = {}
        result.statement = text
        if plan is not None:
            result.plan = plan_tree(plan, stats, ids)
        result.advice = advise(statement, text, self.catalog, plan, stats, ids, self.limits)
        return result

    def _context(self, analyze=False):
        return Context(self.catalog, analyze, self.limits)

    def _plan(self, plan):
        return mark_index_only(Optimizer().optimize(plan)) if self.optimize else plan

    def _matching_rows(self, ctx, table, where):
        """(key, row) of every row the WHERE clause selects, through an index when one helps."""
        scan, conditions = TableScan(table, []), [where] if where is not None else []
        if self.optimize and where is not None:
            optimizer = Optimizer()
            optimizer.optimize_expr(where)
            scan, conditions = optimizer.access_path(scan, conjuncts(where))
        pairs = index_scan_pairs(scan, ctx) if isinstance(scan, IndexScan) else self.catalog.scan(table)
        checks = [ctx.compile(c) for c in conditions]
        matches = []
        for key, row in pairs:
            if all(check(row, ()) is True for check in checks):
                ctx.hold()
                matches.append((key, row))
        return matches

    def _update(self, binder, statement):
        table, where, assignments = binder.bind_update(statement)
        ctx = self._context()
        targets = self._matching_rows(ctx, table, where)
        compiled = [(pos, ctx.compile(e)) for pos, e in assignments]
        updated = []
        for _, row in targets:
            new = list(row)
            for pos, f in compiled:
                new[pos] = f(row, ())
            updated.append(self.catalog.check_row(table, new))
        # Remove every old version before writing new ones, so swapping key values
        # between rows (UPDATE t SET id = id + 1) is not mistaken for a duplicate.
        for key, row in targets:
            self.catalog.delete_row(table, key, row)
        for (key, _), row in zip(targets, updated, strict=True):
            self.catalog.insert_row(table, row, None if table.primary_key else key)
        return Result(rowcount=len(targets), status=f"UPDATE {len(targets)}")

    def _delete(self, binder, statement):
        table, where = binder.bind_delete(statement)
        targets = self._matching_rows(self._context(), table, where)
        for key, row in targets:
            self.catalog.delete_row(table, key, row)
        return Result(rowcount=len(targets), status=f"DELETE {len(targets)}")


