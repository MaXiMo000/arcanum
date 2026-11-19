"""The public entry point: open a database file and run SQL against it."""

import time
from dataclasses import dataclass, field

from ..errors import ArcanumError, LockWaitError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse_statements
from ..storage import Pager, encode
from .advisor import advise
from .binder import Binder
from .catalog import Catalog
from .executor import Context, QueryLimits, index_scan_pairs, run
from .expr import conjuncts
from .mvcc import READ_COMMITTED, REPEATABLE_READ, XID_BATCH, TransactionManager
from .optimizer import Optimizer, mark_index_only
from .plan import IndexScan, TableScan, explain, plan_tree

_WRITES = (n.Insert, n.Update, n.Delete, n.CreateTable, n.DropTable, n.CreateIndex, n.DropIndex, n.Analyze)
_DDL = (n.CreateTable, n.DropTable, n.CreateIndex, n.DropIndex)


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
    planner: list = field(default_factory=list)  # the optimizer's decisions (see optimizer.py), when traced
    planning_ms: float | None = None  # binding and optimizing, when traced
    execution_ms: float | None = None  # running the plan, when traced


class Database:
    """A database stored in one file.

    Every statement is atomic: if it fails, none of its changes remain.
    BEGIN ... COMMIT groups statements; ROLLBACK undoes the group. A commit is
    durable once it returns: it is in the write-ahead log, and opening the
    database after a crash replays every committed transaction and drops the rest.

    Several sessions can share one database (connect()); execute() runs on a
    default one. Concurrency control is PostgreSQL's: rows keep versions, each
    statement reads through a snapshot (READ COMMITTED: taken per statement;
    REPEATABLE READ: once per transaction), readers never wait for writers, and
    a write to a row another open transaction changed fails with LockWaitError
    where PostgreSQL would wait. Sessions take turns: one statement runs at a time.

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
        self.mvcc = TransactionManager(self.catalog.clog, self.catalog.meta.get("xid_limit", 1))
        self.catalog.mvcc = self.mvcc
        self.connections = {}
        self.exclusive = None  # the session whose open transaction changed the schema; others wait for it
        self._snapshot = None  # what the running statement sees
        self._txn = None  # the transaction the running statement writes in
        self._main = self.connect("main")
        if self.catalog.meta.get("unsettled"):
            # The last session ended with rows still holding versions (a crash, most likely): settle them
            # now that nothing is running, dropping whatever unfinished transactions left behind.
            self.catalog.save_commit_log(self.mvcc.committed & self.catalog.vacuum(self.mvcc.next_xid))
        self.pager.commit()

    @property
    def recovered_frames(self):
        """How many committed log frames were replayed when this database was opened."""
        return self.pager.recovered_frames

    @property
    def in_transaction(self):
        """Whether the default session has a transaction open."""
        return self._main.in_transaction

    def connect(self, name=None):
        """A new session on this database, with its own transactions."""
        name = name or f"s{len(self.connections) + 1}"
        if name in self.connections:
            raise ValueError(f"a session named {name} already exists")
        conn = self.connections[name] = Connection(self, name)
        return conn

    def execute(self, sql, trace=False, max_rows=None):
        """Run one statement in the default session and return its Result.

        trace=True also returns the plan of a query or EXPLAIN as a tree, with
        each operator's actual rows and time. max_rows keeps only the first rows
        of a query's result (the rest are counted in row_total, not stored).
        """
        return self._main.execute(sql, trace, max_rows)

    def execute_script(self, sql, trace=False, max_rows=None):
        """Run several semicolon-separated statements in the default session; returns one Result per statement.

        If one fails, the error carries `completed` (how many statements before
        it ran, and stay run) and `statement_start` (its offset in sql).
        """
        return self._main.execute_script(sql, trace, max_rows)

    def close(self):
        for conn in list(self.connections.values()):
            conn.close()
        self.pager.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- statement dispatch ---------------------------------------------------------

    def _execute(self, conn, statement, text, sql, trace=False, max_rows=None):
        if self.exclusive is not None and self.exclusive is not conn:
            raise LockWaitError(f"session {self.exclusive.name} changed the schema in a transaction that is still "
                                "open; PostgreSQL would make this statement wait until it commits or rolls back")
        if isinstance(statement, n.Begin):
            if conn.txn is not None:
                raise SqlError("a transaction is already in progress")
            conn.txn = self._begin(conn, statement.isolation or READ_COMMITTED)
            return Result(status="BEGIN")
        if isinstance(statement, (n.Commit, n.Rollback)):
            if conn.txn is None:
                raise SqlError("there is no transaction in progress")
            commit = isinstance(statement, n.Commit)
            self._end(conn, conn.txn, commit)
            return Result(status="COMMIT" if commit else "ROLLBACK")

        if isinstance(statement, _DDL):
            others = [c.name for c in self.connections.values() if c is not conn and c.txn is not None]
            if others:
                raise LockWaitError(f"session {others[0]} has a transaction open; PostgreSQL would make this schema "
                                    "change wait until it ends")
            if conn.txn is not None and not conn.ddl:
                # Schema changes are not versioned: undo them, if the transaction rolls back, by restoring pages.
                # No other session may run meanwhile, so the restore cannot touch anyone else's work.
                self.pager.savepoint()
                conn.ddl = True
                self.exclusive = conn
        txn, autocommit = conn.txn, False
        if txn is None and isinstance(statement, _WRITES):
            txn, autocommit = self._begin(conn, READ_COMMITTED), True
        self._txn, self._snapshot = txn, self._take_snapshot(txn)
        self.pager.savepoint()
        try:
            result = self._run(statement, text, sql, trace, max_rows)
        except BaseException:
            self.pager.rollback()
            self.catalog.reload()
            if autocommit:
                self._end(conn, txn, commit=False)
            raise
        finally:
            self._txn = self._snapshot = None
        self.pager.release()
        if autocommit:
            self._end(conn, txn, commit=True)
        return result

    def _begin(self, conn, isolation):
        if self.mvcc.next_xid >= self.catalog.meta.get("xid_limit", 0):
            # Reserve the next batch of ids on disk first, so none is handed out twice after a crash.
            self.catalog.set_meta("xid_limit", self.mvcc.next_xid + XID_BATCH)
        return self.mvcc.begin(conn.name, isolation)

    def _take_snapshot(self, txn):
        if txn is None:
            return self.mvcc.snapshot()
        if txn.isolation == REPEATABLE_READ:
            if txn.snapshot is None:
                txn.snapshot = self.mvcc.snapshot(txn.xid)
            return txn.snapshot
        return self.mvcc.snapshot(txn.xid)

    def _end(self, conn, txn, commit):
        """Commit or roll back txn, then settle every row whose versions nobody needs any more."""
        if conn.ddl:
            if commit:
                self.pager.release()
            else:
                self.pager.rollback()
                self.catalog.reload()
            conn.ddl = False
            self.exclusive = None
        self.mvcc.end(txn, commit)
        if conn.txn is txn:
            conn.txn = None
        # The commit itself: versions the transaction made are frozen where no snapshot needs them, and
        # the commit log keeps its id only while some version still mentions it.
        mentioned = self.catalog.prune(self.mvcc.horizon())
        self.catalog.save_commit_log(self.mvcc.committed & mentioned)
        self.pager.commit()

    def _run(self, statement, text, sql, trace=False, max_rows=None):
        binder = Binder(self.catalog, sql)
        if isinstance(statement, n.Query):
            started, decisions = time.perf_counter(), [] if trace else None
            plan = self._plan(binder.bind_query(statement), decisions)
            planned = time.perf_counter()
            ctx = self._context(analyze=trace)
            rows, total = [], 0
            for row in run(plan, ctx):
                total += 1
                if max_rows is None or total <= max_rows:
                    rows.append(row)
            result = Result([c.name for c in plan.columns], rows, status=f"SELECT {total}", row_total=total)
            if not trace:
                return result
            result.planning_ms = (planned - started) * 1000
            result.execution_ms = (time.perf_counter() - planned) * 1000
            return self._traced(result, statement, text, plan, ctx.stats, decisions)
        if isinstance(statement, n.Explain):
            if not isinstance(statement.statement, n.Query):
                raise SqlError("Arcanum doesn't support EXPLAIN for statements other than queries yet")
            started, decisions = time.perf_counter(), [] if trace else None
            plan = self._plan(binder.bind_query(statement.statement), decisions)
            planned = time.perf_counter()
            stats = None
            if statement.analyze:
                ctx = self._context(analyze=True)
                for _ in run(plan, ctx):
                    pass
                stats = ctx.stats
            result = Result(["plan"], [(line,) for line in explain(plan, stats)], status="EXPLAIN")
            if not trace:
                return result
            result.planning_ms = (planned - started) * 1000
            if statement.analyze:
                result.execution_ms = (time.perf_counter() - planned) * 1000
            return self._traced(result, statement, text, plan, stats, decisions)
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
                self.catalog.analyze(table, self._snapshot)
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
            self.catalog.insert_row(table, self.catalog.check_row(table, row), self._txn)
        return Result(rowcount=len(rows), status=f"INSERT {len(rows)}")

    def _traced(self, result, statement, text, plan=None, stats=None, decisions=None):
        """Attach the plan tree (with actual rows and time, if measured), the planner's decisions and the advisor's hints."""
        ids = {}
        result.statement = text
        if plan is not None:
            result.plan = plan_tree(plan, stats, ids)
            result.planner = [{**d, "node": ids.get(id(d["node"])) if d["node"] is not None else None}
                              for d in decisions or []]
        result.advice = advise(statement, text, self.catalog, plan, stats, ids, self.limits)
        return result

    def _context(self, analyze=False):
        return Context(self.catalog, analyze, self.limits, self._snapshot)

    def _plan(self, plan, decisions=None):
        """Optimize a bound plan; decisions, if a list, receives the optimizer's reasoning."""
        if not self.optimize:
            return plan
        return mark_index_only(Optimizer(decisions).optimize(plan), decisions)

    def _matching_rows(self, ctx, table, where):
        """(key, row) of every row the WHERE clause selects, through an index when one helps."""
        scan, conditions = TableScan(table, []), [where] if where is not None else []
        if self.optimize and where is not None:
            optimizer = Optimizer()
            optimizer.optimize_expr(where)
            scan, conditions = optimizer.access_path(scan, conjuncts(where))
        pairs = index_scan_pairs(scan, ctx) if isinstance(scan, IndexScan) else self.catalog.scan(table, ctx.snapshot)
        checks = [ctx.compile(c) for c in conditions]
        matches = []
        for key, row in pairs:
            if all(check(row, ()) is True for check in checks):
                ctx.hold()
                matches.append((key, row))
        return matches

    def _locked_rows(self, ctx, table, where):
        """The rows a WHERE clause selects, each checked writable (see Catalog.lock_row):
        [(key, row as seen, versions, position of the version to change)]."""
        locked = []
        for key, row in self._matching_rows(ctx, table, where):
            hit = self.catalog.lock_row(table, key, self._txn, ctx.snapshot)
            if hit is not None:
                locked.append((key, row, *hit))
        return locked

    def _update(self, binder, statement):
        table, where, assignments = binder.bind_update(statement)
        ctx = self._context()
        targets = self._locked_rows(ctx, table, where)
        compiled = [(pos, ctx.compile(e)) for pos, e in assignments]
        stayed, moved = [], []
        for key, row, versions, current in targets:
            new = list(row)
            for pos, f in compiled:
                new[pos] = f(row, ())
            new = self.catalog.check_row(table, new)
            if not table.primary_key or encode(tuple(new[i] for i in table.primary_key)) == key:
                stayed.append((key, versions, current, new))
            else:
                moved.append((key, versions, current, new))
        # Every old version is marked deleted before uniqueness is checked, so swapping keys or unique
        # values between rows (UPDATE t SET id = id + 1) is not mistaken for a duplicate.
        for key, versions, current, new in stayed:
            self.catalog.update_version(table, key, versions, current, new, self._txn)
        for key, versions, current, _ in moved:
            self.catalog.delete_version(table, key, versions, current, self._txn)
        for _, _, _, new in moved:
            self.catalog.insert_row(table, new, self._txn)
        for key, _, _, new in stayed:
            self.catalog.check_unique(table, new, key, self._txn)
        return Result(rowcount=len(targets), status=f"UPDATE {len(targets)}")

    def _delete(self, binder, statement):
        table, where = binder.bind_delete(statement)
        targets = self._locked_rows(self._context(), table, where)
        for key, _, versions, current in targets:
            self.catalog.delete_version(table, key, versions, current, self._txn)
        return Result(rowcount=len(targets), status=f"DELETE {len(targets)}")




class Connection:
    """A session on a Database: its own transaction, isolation level and snapshots over shared data."""

    def __init__(self, db, name):
        self.db = db
        self.name = name
        self.txn = None  # the open transaction (from BEGIN), if any
        self.ddl = False  # whether that transaction changed the schema (and so holds a page savepoint)

    @property
    def in_transaction(self):
        return self.txn is not None

    def execute(self, sql, trace=False, max_rows=None):
        statements = parse_statements(sql)
        if len(statements) != 1:
            raise SqlError(f"execute() runs exactly one statement, got {len(statements)}; use execute_script()")
        statement, text = statements[0]
        return self.db._execute(self, statement, text, sql, trace, max_rows)

    def execute_script(self, sql, trace=False, max_rows=None):
        results, cursor = [], 0
        for statement, text in parse_statements(sql):
            start = sql.find(text, cursor)
            cursor = start + len(text)
            try:
                results.append(self.db._execute(self, statement, text, sql, trace, max_rows))
            except ArcanumError as e:
                e.completed, e.statement_start = len(results), start
                raise
        return results

    def close(self):
        """End the session, rolling back its open transaction."""
        if self.txn is not None:
            self.db._end(self, self.txn, commit=False)
        self.db.connections.pop(self.name, None)
