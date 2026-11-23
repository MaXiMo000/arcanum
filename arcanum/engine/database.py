"""The public entry point: open a database file and run SQL against it."""

import re
import time
from dataclasses import dataclass, field, replace
from decimal import DecimalException

from ..errors import ArcanumError, ExecutionError, LockWaitError, SqlError
from ..sql import nodes as n
from ..sql.parser import parse_statements
from ..storage import Pager
from ..values import Jsonb
from .advisor import Workload, advise
from .binder import Binder
from .catalog import Catalog
from .csvimport import import_sql, plan_import
from .ddl import Alter
from .dml import Writer
from .executor import Context, QueryLimits, index_scan_pairs, run
from .expr import conjuncts, to_text
from .mvcc import READ_COMMITTED, REPEATABLE_READ, XID_BATCH, TransactionManager
from .optimizer import Optimizer, mark_index_only
from .plan import IndexScan, TableScan, explain, plan_tree
from .ssi import SERIALIZABLE, SSI

_DDL = (n.CreateTable, n.CreateTableAs, n.DropTable, n.CreateIndex, n.DropIndex, n.CreateView, n.DropView,
        n.AlterTable)
_WRITES = (n.Insert, n.Update, n.Delete, n.Truncate, n.Analyze, n.Vacuum, *_DDL)
_CHANGES = tuple(s for s in _WRITES if s is not n.Analyze)  # what a READ ONLY transaction refuses
VACUUM_COLUMNS = ["table", "rows", "versions_removed", "old_versions_kept", "bytes_before", "bytes_after",
                  "pages_before", "pages_after", "kept_for"]


@dataclass
class Result:
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    rowcount: int = -1  # rows changed by INSERT, UPDATE or DELETE
    status: str = ""  # e.g. "INSERT 3", "CREATE TABLE"
    plan: dict | None = None  # the query plan as a tree (see plan.plan_tree), when traced
    row_total: int = 0  # rows the query produced; more than len(rows) when max_rows cut them off
    clipped: int = 0  # values shortened to Database.max_value_chars
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
                 optimize=True, limits=None, max_pages=None, pool_policy="lru"):
        self.optimize = optimize
        self.limits = limits or QueryLimits()
        self.pager = Pager(path, pool_size, events, fs, checkpoint_frames, max_pages, pool_policy)
        self.events = self.pager.events
        self.catalog = Catalog(self.pager, max_keys)
        self.mvcc = TransactionManager(self.catalog.clog, self.catalog.meta.get("xid_limit", 1))
        self.catalog.mvcc = self.mvcc
        self.ssi = self.catalog.ssi = SSI(
            self.mvcc, lambda table, key: self.catalog._describe(self.catalog.tables[table], key), self.events.emit)
        self.connections = {}
        self.workload = Workload()  # what the advisor saw across statements, for index_report()
        self.exclusive = None  # the session whose open transaction changed the schema; others wait for it
        self._snapshot = None  # what the running statement sees
        self._deadline = None  # when the running statement times out (time.monotonic()), if it can
        # Longest value kept in a result, as text (None: whole values). A server sets it, so a query can
        # never make it hold or send gigabytes; the database itself stores and computes with whole values.
        self.max_value_chars = None
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

    def import_csv(self, table, text, header=True):
        """Load CSV text into `table`, creating it with inferred column types if it does not exist
        (engine/csvimport.py). Returns the Results of the CREATE TABLE (if any) and the INSERT."""
        return self.execute_script(import_sql(plan_import(self.catalog, table, text, header)))

    def index_report(self):
        """Every index with its scans since the database was opened and whether another index makes it
        redundant, and the indexes the advisor suggested across traced statements (advisor.Workload)."""
        return self.workload.report(self.catalog)

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
                                "open; PostgreSQL would make this statement wait until it commits or rolls back",
                                self.exclusive.txn.xid, "the schema")
        if isinstance(statement, n.Begin):
            if conn.txn is not None:
                raise SqlError("a transaction is already in progress")
            conn.txn = self._begin(conn, statement.isolation or READ_COMMITTED)
            conn.read_only = statement.read_only
            return Result(status="BEGIN")
        if isinstance(statement, (n.Commit, n.Rollback)):
            if conn.txn is None:
                raise SqlError("there is no transaction in progress")
            commit = isinstance(statement, n.Commit)
            self._end(conn, conn.txn, commit)
            return Result(status="COMMIT" if commit else "ROLLBACK")
        if isinstance(statement, (n.Savepoint, n.RollbackTo, n.Release)):
            return self._savepoint(conn, statement)
        if conn.txn is not None and conn.read_only and isinstance(statement, _CHANGES):
            raise SqlError(f"cannot execute {_command(statement)} in a read-only transaction")

        if isinstance(statement, n.Vacuum) and conn.txn is not None:
            raise SqlError("VACUUM cannot run inside a transaction block")
        if isinstance(statement, _DDL) or (isinstance(statement, n.Vacuum) and statement.full):
            others = [c for c in self.connections.values() if c is not conn and c.txn is not None]
            if others:
                raise LockWaitError(f"session {others[0].name} has a transaction open; PostgreSQL would make this "
                                    "schema change wait until it ends", others[0].txn.xid, "the tables it uses")
            if conn.txn is not None and not conn.ddl:
                # Schema changes are not versioned: undo them, if the transaction rolls back, by restoring pages.
                # No other session may run meanwhile, so the restore cannot touch anyone else's work.
                self.pager.savepoint()
                conn.ddl = True
                conn.ddl_at = len(conn.savepoints)  # savepoints older than this cannot undo the schema change
                self.exclusive = conn
        txn, autocommit = conn.txn, False
        if txn is None and isinstance(statement, _WRITES):
            txn, autocommit = self._begin(conn, READ_COMMITTED), True
        self._txn, self._snapshot = txn, self._take_snapshot(txn)
        timeout = self.limits.timeout
        self._deadline = time.monotonic() + timeout if timeout is not None else None
        self.pager.savepoint()
        try:
            result = self._run(statement, text, sql, trace, max_rows)
        except BaseException as e:
            self.pager.rollback()
            self.catalog.reload()
            if autocommit or (txn is not None and getattr(e, "rolled_back", None) is txn):
                self._end(conn, txn, commit=False)  # an SSI failure ends the whole transaction
            mapped = _as_sql_error(e)
            if mapped is not e:
                raise mapped from e
            raise
        finally:
            self._txn = self._snapshot = None
        self.pager.release()
        if autocommit:
            self._end(conn, txn, commit=True)
        return result

    def clip(self, row):
        """(row with every value longer than max_value_chars shortened, as text, number of values shortened)."""
        limit = self.max_value_chars
        if limit is None:
            return row, 0
        out, clipped = [], 0
        for v in row:
            if isinstance(v, (str, tuple, Jsonb)):
                text = v if isinstance(v, str) else to_text(v)
                if len(text) > limit:
                    v, clipped = f"{text[:limit]}… ({len(text):,} characters in all)", clipped + 1
            out.append(v)
        return tuple(out), clipped

    def _reserve_xid(self):
        if self.mvcc.next_xid >= self.catalog.meta.get("xid_limit", 0):
            # Reserve the next batch of ids on disk first, so none is handed out twice after a crash.
            self.catalog.set_meta("xid_limit", self.mvcc.next_xid + XID_BATCH)

    def _savepoint(self, conn, statement):
        """SAVEPOINT, ROLLBACK TO SAVEPOINT and RELEASE SAVEPOINT, as subtransactions (see mvcc.py)."""
        txn, name = conn.txn, statement.name
        if txn is None:
            raise SqlError(f"{type(statement).__name__.upper()} can only be used in a transaction block")
        if isinstance(statement, n.Savepoint):
            self._reserve_xid()
            xid = self.mvcc.subtransaction(txn)
            self.ssi.savepoint(txn, xid)
            conn.savepoints.append({"name": name, "xid": xid, "xids": [xid]})
            self.events.emit("savepoint", session=conn.name, name=name, xid=xid)
            return Result(status="SAVEPOINT")
        index = next((i for i in range(len(conn.savepoints) - 1, -1, -1) if conn.savepoints[i]["name"] == name), None)
        if index is None:
            raise SqlError(f"savepoint {name} does not exist")
        if isinstance(statement, n.Release):
            # Its ids (and those of the savepoints inside it) now belong to the enclosing one.
            released = [x for sp in conn.savepoints[index:] for x in sp["xids"]]
            del conn.savepoints[index:]
            if conn.savepoints:
                conn.savepoints[-1]["xids"] += released
            txn.current = conn.savepoints[-1]["xid"] if conn.savepoints else txn.xid
            self.events.emit("release", session=conn.name, name=name)
            return Result(status="RELEASE")
        if conn.ddl and index < conn.ddl_at:
            raise SqlError(f"Arcanum can't roll back a schema change to savepoint {name} yet; roll back the "
                           "whole transaction")
        aborted = [x for sp in conn.savepoints[index:] for x in sp["xids"]]
        self.mvcc.abort(txn, aborted)
        del conn.savepoints[index + 1:]
        self._reserve_xid()
        xid = self.mvcc.subtransaction(txn)  # the savepoint stays, with a fresh id, as in PostgreSQL
        self.ssi.savepoint(txn, xid)
        conn.savepoints[index] = {"name": name, "xid": xid, "xids": [xid]}
        self.events.emit("rollback_to", session=conn.name, name=name, aborted=aborted)
        return Result(status="ROLLBACK")

    def _begin(self, conn, isolation):
        self._reserve_xid()
        txn = self.mvcc.begin(conn.name, isolation)
        self.ssi.begin(txn)
        return txn

    def _take_snapshot(self, txn):
        if txn is None:
            return self.mvcc.snapshot()
        if txn.isolation in (REPEATABLE_READ, SERIALIZABLE):
            if txn.snapshot is None:
                txn.snapshot = self.mvcc.snapshot(txn)
            # The same snapshot all transaction long, seeing its own savepoints' writes too.
            return replace(txn.snapshot, own=frozenset(txn.own))
        return self.mvcc.snapshot(txn)

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
        self.ssi.end(txn, commit)
        if conn.txn is txn:
            conn.txn = None
            conn.savepoints = []
            conn.read_only = False
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
            rows, total, clipped = [], 0, 0
            for row in run(plan, ctx):
                total += 1
                if max_rows is None or total <= max_rows:
                    row, shortened = self.clip(row)
                    clipped += shortened
                    rows.append(row)
            result = Result([c.name for c in plan.columns], rows, status=f"SELECT {total}", row_total=total,
                            clipped=clipped)
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
            return Writer(self, binder).insert(statement)
        if isinstance(statement, (n.Update, n.Delete)):
            if statement.alias:
                binder.target_aliases[statement.table] = statement.alias
            writer = Writer(self, binder)
            result = writer.update(statement) if isinstance(statement, n.Update) else writer.delete(statement)
            return self._traced(result, statement, text) if trace else result
        if isinstance(statement, n.Truncate):
            return self._truncate(binder, statement)
        if isinstance(statement, n.CreateTableAs):
            return self._create_table_as(binder, statement)
        if isinstance(statement, n.CreateTable):
            if statement.name in self.catalog.tables and statement.if_not_exists:
                return Result(status="CREATE TABLE")
            table = self.catalog.create_table(statement, text)
            Writer(self, binder).validate(table)  # bind CHECKs and default expressions now
            return Result(status="CREATE TABLE")
        if isinstance(statement, n.CreateIndex):
            if statement.name in self.catalog.indexes and statement.if_not_exists:
                return Result(status="CREATE INDEX")
            self.catalog.create_index(statement, text)
            result = Result(status="CREATE INDEX")
            return self._traced(result, statement, text) if trace else result
        if isinstance(statement, n.CreateView):
            return self._create_view(binder, statement, text)
        if isinstance(statement, n.Vacuum):
            return self._vacuum(binder, statement)
        if isinstance(statement, n.AlterTable):
            Alter(self, Writer(self, binder)).run(statement)
            return Result(status="ALTER TABLE")
        if isinstance(statement, n.DropView):
            if statement.name not in self.catalog.views:
                if statement.if_exists:
                    return Result(status="DROP VIEW")
                raise SqlError(f"view {statement.name} does not exist")
            self._check_no_views(statement.name, "view")
            self.catalog.drop_view(statement.name)
            return Result(status="DROP VIEW")
        if isinstance(statement, n.DropTable):
            if statement.name not in self.catalog.tables:
                if statement.if_exists:
                    return Result(status="DROP TABLE")
                raise SqlError(f"table {statement.name} does not exist")
            self._check_no_views(statement.name, "table")
            for child, fk in self.catalog.referencing(statement.name):
                if child.name != statement.name:
                    raise SqlError(f"cannot drop table {statement.name} because foreign key {fk.name} on table "
                                   f"{child.name} depends on it; drop {child.name} first")
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

    def _truncate(self, binder, statement):
        """Empty the tables. Like PostgreSQL, refuse when a table outside the list references one by a foreign
        key, unless CASCADE empties those too; referencing tables are emptied first."""
        names = list(dict.fromkeys(binder.table(name).name for name in statement.tables))
        for name in names:  # grows while CASCADE adds referencing tables
            for child, fk in self.catalog.referencing(name):
                if child.name in names:
                    continue
                if not statement.cascade:
                    raise SqlError(f"cannot truncate table {name}: foreign key {fk.name} on table {child.name} "
                                   f"references it; truncate {child.name} too, or use CASCADE")
                names.append(child.name)
        writer = Writer(self, binder)
        for name in reversed(names):
            writer.delete(n.Delete(name))
        return Result(status="TRUNCATE TABLE")

    def _create_table_as(self, binder, statement):
        """CREATE TABLE ... AS and SELECT ... INTO: a table with the query's column names and types, filled
        with its rows. The table is stored as the plain CREATE TABLE it amounts to."""
        if statement.name in self.catalog.tables and statement.if_not_exists:
            return Result(status="CREATE TABLE AS")
        columns = binder.bind_query(statement.query).columns
        names = list(statement.columns or [])
        if len(names) > len(columns):
            raise SqlError(f"CREATE TABLE AS names {len(names)} columns but the query returns {len(columns)}")
        names += [c.name for c in columns[len(names):]]
        defs = ", ".join(f"{_quoted(name)} {'TEXT' if c.type == 'NULL' else c.type}"
                         for name, c in zip(names, columns, strict=True))
        sql = f"CREATE TABLE {_quoted(statement.name)} ({defs})"
        self.catalog.create_table(parse_statements(sql)[0][0], sql)
        if not statement.with_data:
            return Result(status="CREATE TABLE AS")
        inserted = Writer(self, binder).insert(n.Insert(statement.name, statement.query, names))
        return Result(status=f"SELECT {inserted.status.split()[-1]}")

    def _vacuum(self, binder, statement):
        """Prune every row of the table (or of all tables) against the oldest snapshot still in use; FULL also
        rewrites each table and index compactly. One row per table says what happened and, when old
        versions had to stay, which transaction still needs them."""
        tables = [binder.table(statement.table)] if statement.table else list(self.catalog.tables.values())
        holders = [t for t in self.mvcc.running.values() if t is not self._txn and t.snapshot is not None]
        oldest = min(holders, key=lambda t: t.snapshot.xmin, default=None)
        horizon = self.mvcc.horizon(exclude=self._txn)
        rows = []
        for table in tables:
            done = self.catalog.vacuum_table(table, horizon, statement.full)
            held = f"session {oldest.owner}'s transaction {oldest.xid}" if done["kept"] and oldest else None
            rows.append((table.name, done["rows"], done["removed"], done["kept"], done["bytes_before"],
                         done["bytes_after"], done["pages_before"], done["pages_after"], held))
        if statement.analyze:
            for table in tables:
                self.catalog.analyze(table, self._snapshot)
        return Result(VACUUM_COLUMNS, rows, status="VACUUM FULL" if statement.full else "VACUUM", row_total=len(rows))

    def _create_view(self, binder, statement, text):
        name = statement.name
        if name in self.catalog.views and statement.or_replace:
            self.catalog.drop_view(name)
        self.catalog._check_name_free(name)
        plan = binder.bind_query(statement.query)  # a view that does not bind is refused now, not when used
        if statement.columns and len(statement.columns) > len(plan.columns):
            raise SqlError(f"view {name} names {len(statement.columns)} columns but its query has {len(plan.columns)}")
        names = (statement.columns or []) + [c.name for c in plan.columns[len(statement.columns or []):]]
        if len(set(names)) != len(names):
            raise SqlError(f"view {name} has two columns with the same name; give them aliases")
        self.catalog.create_view(name, text)
        return Result(status="CREATE VIEW")

    def _check_no_views(self, name, kind):
        dependents = self.catalog.dependent_views(name)
        if dependents:
            raise SqlError(f"cannot drop {kind} {name} because view {dependents[0]} depends on it; drop the view first")

    def _traced(self, result, statement, text, plan=None, stats=None, decisions=None):
        """Attach the plan tree (with actual rows and time, if measured), the planner's decisions and the advisor's hints."""
        ids = {}
        result.statement = text
        if plan is not None:
            result.plan = plan_tree(plan, stats, ids)
            result.planner = [{**d, "node": ids.get(id(d["node"])) if d["node"] is not None else None}
                              for d in decisions or []]
        result.advice = advise(statement, text, self.catalog, plan, stats, ids, self.limits)
        self.workload.record(result.advice, text)
        return result

    def _context(self, analyze=False):
        return Context(self.catalog, analyze, self.limits, self._snapshot, self._deadline)

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


def _command(statement):
    """The SQL command a statement node stands for: CreateTableAs -> CREATE TABLE AS."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", type(statement).__name__).upper()


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _as_sql_error(e):
    """The SQL error for a Python error a statement can provoke with legal SQL; other errors unchanged."""
    if isinstance(e, RecursionError):
        return ExecutionError("the statement, or a value in it, is nested too deeply")
    if isinstance(e, MemoryError):
        return ExecutionError("out of memory: the statement needs more memory than this server allows")
    if isinstance(e, (OverflowError, DecimalException)):
        return ExecutionError("value out of range")
    return e


class Connection:
    """A session on a Database: its own transaction, isolation level and snapshots over shared data."""

    def __init__(self, db, name):
        self.db = db
        self.name = name
        self.txn = None  # the open transaction (from BEGIN), if any
        self.ddl = False  # whether that transaction changed the schema (and so holds a page savepoint)
        self.ddl_at = 0  # how many savepoints existed when it first did
        self.savepoints = []  # [{"name", "xid": the id it writes with, "xids": every id it owns}], oldest first
        self.read_only = False  # whether that transaction began READ ONLY

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
