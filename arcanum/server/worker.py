"""The engine worker: one process per session, owning that session's database.

The server talks to it over a pipe (one request, one reply) and receives the
engine's events over a second pipe, in batches. Every message is JSON with a
size cap, never pickle, so even a misbehaving worker cannot make the server
run code. Running each database in its own process is what lets the server
kill it mid-write to demonstrate crash recovery, and stop a runaway
statement without affecting anyone else.
"""

import json
import logging
import secrets
import sys
import time
from collections import Counter

from ..engine import Database, QueryLimits
from ..engine.doctor import diagnose
from ..engine.expr import to_text
from ..errors import (
    ArcanumError,
    ConstraintError,
    DeadlockError,
    ExecutionError,
    LockTimeoutError,
    LockWaitError,
    SqlError,
)
from ..events import EventBus
from ..lab import compare
from ..sql.parser import parse_statements
from ..storage import PAGE_SIZE, decode

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
log = logging.getLogger("arcanum.server.worker")

SEED_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
CREATE INDEX orders_customer ON orders (customer_id);
INSERT INTO customers
    SELECT n, 'Customer ' || n, CASE n % 4 WHEN 0 THEN 'Lisbon' WHEN 1 THEN 'Osaka' WHEN 2 THEN 'Quito' ELSE 'Oslo' END
    FROM (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 24) SELECT n FROM s) AS seq;
INSERT INTO orders
    SELECT n, (n * 7) % 24 + 1, (n * 37) % 500 + 0.5, CASE WHEN n % 5 = 0 THEN 'open' ELSE 'shipped' END
    FROM (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 80) SELECT n FROM s) AS seq;
ANALYZE;
"""

CRASH_TABLE = "CREATE TABLE IF NOT EXISTS crash_demo (id INTEGER PRIMARY KEY, batch INTEGER, payload TEXT)"
_FLUSH_NOW = {"commit", "recovery", "checkpoint", "rollback", "demo_commit"}
# Errors whose messages describe the user's SQL; any other error is reported without details.
_USER_ERRORS = (SqlError, ConstraintError, ExecutionError)


def send(conn, message):
    conn.send_bytes(json.dumps(message, allow_nan=False, separators=(",", ":")).encode())


def receive(conn):
    return json.loads(conn.recv_bytes(MAX_MESSAGE_BYTES))


class EventPump:
    """Collects engine events and sends them to the server in batches."""

    def __init__(self, conn, limit):
        self.conn = conn
        self.limit = limit  # events per request; beyond it they are counted, not sent
        self.buffer = []
        self.seq = 0
        self.count = 0
        self.kinds = Counter()  # every event of the current request, counted even past the limit
        self.notes = []
        self.started = time.perf_counter()
        self.request = 0  # numbers each request, so a viewer can replay one request's events

    def start_request(self):
        self.request += 1
        self.count = 0
        self.kinds = Counter()
        self.notes = []  # constraint checks, cascades, sequences and conflicts, for the explanation
        self.started = time.perf_counter()

    def request_stats(self, kinds=None, elapsed=None):
        """What the engine did for the current request (or the given counts and seconds), for explanations
        and lesson checks."""
        k = self.kinds if kinds is None else kinds
        seconds = time.perf_counter() - self.started if elapsed is None else elapsed
        return {
            "elapsed_ms": round(seconds * 1000, 2),
            "cache_hits": k["read:cache"],
            "disk_reads": k["read:disk"],
            "nodes_visited": k["node_visit"],
            "pages_written": k["page_write"],
            "pages_allocated": k["page_alloc"],
            "pages_freed": k["page_free"],
            "evictions": k["cache_evict"],
            "splits": k["node_split"],
            "merges": k["node_merge"] + k["node_redistribute"],
            "root_grew": k["root_grow"],
            "root_shrank": k["root_shrink"],
            "log_frames": k["wal_append"],
            "commits": k["commit"],
            "checkpoints": k["checkpoint"],
            "rollbacks": k["rollback"],
            "sort_spills": k["sort_spill"],
            "constraints": self.notes[:NOTES],
        }

    def on_event(self, kind, data):
        self.kinds[kind] += 1
        if kind in ("constraint", "cascade", "sequence", "conflict"):
            self.notes.append({"kind": kind, **data})
        if kind == "page_read":
            self.kinds["read:cache" if data.get("source") == "cache" else "read:disk"] += 1
        self.count += 1
        if self.count > self.limit:
            if self.count == self.limit + 1:
                self.buffer.append({"kind": "events_truncated", "limit": self.limit})
            return
        self.seq += 1
        self.buffer.append({"seq": self.seq, "request": self.request, "kind": kind,
                            **{k: _json_value(v) for k, v in data.items()}})
        if len(self.buffer) >= 256 or kind in _FLUSH_NOW:
            self.flush()

    def flush(self):
        if self.buffer:
            send(self.conn, self.buffer)
            self.buffer = []


def _json_value(v):
    if isinstance(v, bytes):
        try:
            return [_cell(x) for x in decode(v)]  # B+tree keys use the value codec
        except (ValueError, IndexError):
            return v.hex()
    if isinstance(v, tuple):
        return [_cell(x) for x in v]
    return _cell(v)


def _cell(v):
    """A value as JSON: dates, timestamps, numerics and intervals as their SQL text (as PostgreSQL clients
    receive them), so no precision is lost to floats."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return to_text(v)


def error_json(e):
    if isinstance(e, _USER_ERRORS):
        error = {"type": type(e).__name__, "message": getattr(e, "message", str(e))}
        if isinstance(e, SqlError) and e.line is not None:
            error.update(line=e.line, column=e.col)
        if getattr(e, "completed", 0):
            error["completed"] = e.completed  # statements before the failing one, which did run
        if getattr(e, "doctor", None):
            error["doctor"] = e.doctor
        if getattr(e, "dependencies", None):
            error["dependencies"] = e.dependencies  # the rw-conflicts SERIALIZABLE refused
        return error
    # Anything else is a fault on our side. Keep the details (which may include file paths) in the
    # server log, and give the client only a reference to them.
    reference = secrets.token_hex(4)
    log.error("internal error %s: %r", reference, e, exc_info=e)
    return {"type": "InternalError", "message": f"internal error (reference {reference})"}


def _limit_memory(megabytes):
    """Cap the worker's address space where the OS supports it (Linux and other POSIX systems)."""
    try:
        import resource  # noqa: PLC0415 (POSIX only, so imported where it is used)
    except ImportError:
        return  # Windows: the per-statement row and spill budgets still apply
    limit = megabytes * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def worker_main(path, conn, event_conn, options):
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    _limit_memory(options["memory_mb"])
    pump = EventPump(event_conn, options["event_limit"])
    bus = EventBus()
    bus.subscribe(pump.on_event)
    limits = QueryLimits(sort_memory_rows=options["sort_memory_rows"], memory_rows=options["memory_rows"],
                         spill_bytes=options["spill_bytes"], recursive_rows=options["recursive_rows"])
    try:
        db = Database(path, events=bus, max_keys=options["max_keys"], max_pages=options["max_pages"],
                      pool_size=options["pool_size"], limits=limits)
        if not db.catalog.tables:
            db.execute_script(SEED_SQL)
        for name in SESSIONS:
            db.connect(name)
    except Exception as e:
        pump.flush()
        send(conn, {"ready": False, "error": error_json(e)})
        return
    pump.flush()
    send(conn, {"ready": True, "recovered_frames": db.recovered_frames})
    jobs = Jobs(db, pump, options["lock_timeout"])
    handlers = {
        "query": jobs.start,
        "ticket": lambda m: jobs.ticket(m["ticket"]),
        "cancel": lambda m: jobs.cancel(m["ticket"]),
        "state": lambda m: _state(db, m, jobs),
        "workload": lambda m: _workload(db, m, conn, pump),
        "lab": lambda m: {"ok": True, **compare(**m["params"])},
    }
    while True:
        try:
            message = receive(conn)
        except (EOFError, OSError):
            return
        if message.get("op") == "close":
            db.close()
            send(conn, {"ok": True})
            return
        pump.start_request()
        try:
            jobs.resume()  # lock timeouts, and holders that ended since
            reply = handlers[message["op"]](message)
            jobs.resume()  # this request may have released a lock someone waits for
        except ArcanumError as e:
            reply = _failure(db, e, message.get("sql", ""))
        except Exception as e:
            reply = {"ok": False, "error": error_json(e)}
        pump.flush()
        try:
            send(conn, reply)
        except ValueError as e:  # a value JSON cannot carry
            send(conn, {"ok": False, "error": error_json(e)})


SESSIONS = ("A", "B")  # the database sessions each visitor can type into, side by side
NOTES = 50  # constraint notes kept per request
VERSION_ROWS = 40  # rows with versions the state shows at most


class Job:
    """A script one session sent, which may stop at a statement that has to wait for a lock."""

    def __init__(self, ticket, conn, sql, max_rows):
        self.ticket, self.conn, self.sql, self.max_rows = ticket, conn, sql, max_rows
        self.statements, cursor = [], 0
        for statement, text in parse_statements(sql):
            start = sql.find(text, cursor)
            cursor = start + len(text)
            self.statements.append((statement, text, start))
        self.index = 0  # the next statement to run
        self.results = []
        self.kinds = Counter()  # engine events of every run of this job
        self.elapsed = 0.0  # seconds spent running (not waiting)
        self.holder = None  # the transaction it waits for, while parked
        self.wake = None  # the id whose end lets it go on (the holder's, or one of its savepoints')
        self.what = ""
        self.since = None  # when it started waiting (monotonic)
        self.deadline = None


class Jobs:
    """Statements parked behind a lock, the way a PostgreSQL backend waits for another transaction.

    A statement that needs a row another open transaction holds is parked: its
    request answers at once with a ticket, and the session's script resumes
    from that statement as soon as the holder commits or rolls back. Waits form
    a graph between transactions; a wait that would close a cycle is a
    deadlock, and as in PostgreSQL the transaction that detects it is rolled
    back. A wait longer than the lock timeout is cancelled.
    """

    def __init__(self, db, pump, lock_timeout):
        self.db, self.pump, self.lock_timeout = db, pump, lock_timeout
        self.parked = {}  # ticket -> Job
        self.finished = {}  # ticket -> its final reply, until fetched
        self.tickets = 0

    def start(self, message):
        conn = self.db.connections[message.get("session") or SESSIONS[0]]
        waiting = next((j for j in self.parked.values() if j.conn is conn), None)
        if waiting is not None:
            raise ExecutionError(f"session {conn.name} is still waiting for a lock (ticket {waiting.ticket}); wait for "
                                 "it to finish or cancel it first")
        self.tickets += 1
        return self.run(Job(f"t{self.tickets}", conn, message["sql"], message["max_rows"]))

    def run(self, job):
        """Run the job from its next statement: its final reply, or a waiting reply if it parked."""
        before, started = Counter(self.pump.kinds), time.perf_counter()
        try:
            self._advance(job)
        finally:
            job.kinds += self.pump.kinds - before
            job.elapsed += time.perf_counter() - started
        return self._reply(job)

    def _advance(self, job):
        """Run statements until the script ends or one has to wait (the job is then parked)."""
        while job.index < len(job.statements):
            statement, text, start = job.statements[job.index]
            try:
                result = self.db._execute(job.conn, statement, text, job.sql, True, job.max_rows)
            except LockWaitError as e:
                if e.xid is None or self.db.mvcc.status(getattr(e, "wake", e.xid)) != "running":
                    raise
                self._check_deadlock(job, e.xid)
                self._park(job, e)
                return
            except ArcanumError as e:
                e.completed, e.statement_start = job.index, start
                raise
            if job.since is not None:
                self.db.events.emit("lock_granted", session=job.conn.name, waited_ms=self._waited(job))
                job.since = job.holder = job.deadline = None
            job.results.append(_result_json(result))
            job.index += 1

    def resume(self):
        """Resume every parked job whose holder has finished, and cancel those past the lock timeout."""
        progress = True
        while progress:  # one finished job can release another
            progress = False
            for ticket, job in list(self.parked.items()):
                expired = time.monotonic() >= job.deadline
                if not expired and self.db.mvcc.status(job.wake) == "running":
                    continue
                del self.parked[ticket]
                progress = True
                if expired:
                    statement_start = job.statements[job.index][2]
                    error = LockTimeoutError(f"canceling statement due to lock timeout: it waited "
                                             f"{self.lock_timeout:g} seconds for transaction {job.holder}")
                    error.completed, error.statement_start = job.index, statement_start
                    self.db.events.emit("lock_timeout", session=job.conn.name, holder=job.holder)
                    self.finished[ticket] = _failure(self.db, error, job.sql)
                    continue
                try:
                    reply = self.run(job)
                except ArcanumError as e:
                    reply = _failure(self.db, e, job.sql)
                if ticket not in self.parked:
                    self.finished[ticket] = reply

    def ticket(self, ticket):
        if ticket in self.finished:
            return self.finished.pop(ticket)
        job = self.parked.get(ticket)
        if job is None:
            raise ExecutionError(f"no statement is waiting under ticket {ticket}")
        return self._reply(job)

    def cancel(self, ticket):
        job = self.parked.pop(ticket, None)
        if job is None:
            return self.ticket(ticket)
        error = ExecutionError("canceling statement due to user request")
        error.completed, error.statement_start = job.index, job.statements[job.index][2]
        self.db.events.emit("lock_cancel", session=job.conn.name, holder=job.holder)
        return _failure(self.db, error, job.sql)

    def waits(self):
        """Who waits for whom, for the lock graph."""
        out = []
        for job in self.parked.values():
            holder = self.db.mvcc.running.get(job.holder)
            out.append({"ticket": job.ticket, "session": job.conn.name,
                        "xid": job.conn.txn.xid if job.conn.txn else None, "holder": job.holder,
                        "holder_session": holder.owner if holder else None, "what": job.what,
                        "waited_ms": self._waited(job)})
        return out

    # ---- helpers ----------------------------------------------------------------------------

    def _park(self, job, error):
        if job.since is None:
            job.since = time.monotonic()
            job.deadline = job.since + self.lock_timeout
        job.holder, job.what = error.xid, error.what or str(error)
        job.wake = getattr(error, "wake", error.xid)  # a savepoint rolled back releases the row early
        self.parked[job.ticket] = job
        holder = self.db.mvcc.running[error.xid]
        self.db.events.emit("lock_wait", session=job.conn.name, holder=error.xid, holder_session=holder.owner)

    def _check_deadlock(self, job, holder):
        """Follow who waits for whom from the holder; reaching this job's own transaction is a deadlock."""
        waiter = job.conn.txn.xid if job.conn.txn else None
        if waiter is None:
            return  # a statement outside a transaction holds nothing anyone could be waiting for
        chain, x = [], holder
        while x is not None and x not in chain:
            chain.append(x)
            if x == waiter:
                break
            x = next((j.holder for j in self.parked.values() if j.conn.txn and j.conn.txn.xid == x), None)
        if x != waiter:
            return
        cycle = " -> ".join(f"{t} (session {self.db.mvcc.running[t].owner})" for t in [waiter, *chain[:-1]])
        self.db._end(job.conn, job.conn.txn, commit=False)
        self.db.events.emit("deadlock", session=job.conn.name, victim=waiter, cycle=[waiter, *chain[:-1]])
        error = DeadlockError(f"deadlock detected: {cycle} -> {waiter} each wait for the next; transaction {waiter} "
                              "was rolled back so the others can go on")
        error.completed, error.statement_start = job.index, job.statements[job.index][2]
        raise error

    def _waited(self, job):
        return round((time.monotonic() - job.since) * 1000) if job.since is not None else 0

    def _reply(self, job):
        reply = {"ok": True, "stats": self.pump.request_stats(job.kinds, job.elapsed), "results": job.results}
        if job.ticket in self.parked:
            holder = self.db.mvcc.running.get(job.holder)
            reply["waiting"] = {"ticket": job.ticket, "session": job.conn.name, "holder": job.holder,
                                "holder_session": holder.owner if holder else None, "what": job.what,
                                "waited_ms": self._waited(job)}
        return reply


def _result_json(r):
    return {"columns": r.columns, "rows": [[_cell(v) for v in row] for row in r.rows],
            "truncated": r.row_total > len(r.rows),
            "row_total": r.row_total, "rowcount": r.rowcount, "status": r.status, "plan": r.plan,
            "advice": r.advice, "statement": r.statement, "planner": r.planner,
            "planning_ms": r.planning_ms, "execution_ms": r.execution_ms}


def _failure(db, e, sql):
    """The reply for a failed script: the error, with the doctor's diagnosis of SQL mistakes."""
    if isinstance(e, (SqlError, ConstraintError)):
        e.doctor = diagnose(e, db.catalog, sql)
        if e.doctor and e.doctor["fix"]:
            # What to run now: the corrected script from the failing statement on, since
            # the statements before it already ran. Every fix edits at or after that point.
            e.doctor["rerun"] = e.doctor["fix"][getattr(e, "statement_start", 0):]
    return {"ok": False, "error": error_json(e)}


def _state(db, message, jobs):
    """Everything a visualizer needs to draw the database: trees, cache and log."""
    catalog, pager = db.catalog, db.pager
    limit = message.get("node_limit", 2000)

    def tree(kind, name, root, columns, table=None):
        nodes = catalog.btree(root).dump(limit)
        for node in nodes:
            node["keys"] = [list(decode(k)) for k in node["keys"]]
        return {"kind": kind, "name": name, "table": table, "root": root, "key_columns": columns,
                "nodes": nodes, "truncated": len(nodes) >= limit}

    trees = [tree("catalog", "catalog", catalog.tree.root, ["name"])]
    for table in catalog.tables.values():
        key = [table.columns[i].name for i in table.primary_key] or ["rowid"]
        trees.append(tree("table", table.name, table.root, key))
        trees.extend(tree("index", index.name, index.root, [table.columns[i].name for i in index.columns] + key,
                          table.name) for index in table.indexes)
    tables = [{"name": t.name, "columns": [{"name": c.name, "type": c.type} for c in t.columns],
               "rows": t.stats.rows if t.stats else None} for t in catalog.tables.values()]
    return {"ok": True, "tables": tables, "trees": trees, "pager": {
        "page_size": PAGE_SIZE, "page_count": pager.page_count, "max_pages": pager.max_pages,
        "pool_size": pager.pool_size, "cached": [{"page": p, "dirty": d} for p, d in pager.cached_pages()],
        "free": pager.free_pages(), "stats": dict(pager.stats)},
        "wal": {"committed_frames": pager.wal.committed_frames, "uncommitted": pager.wal.has_uncommitted},
        "in_transaction": db.connections[SESSIONS[0]].in_transaction, **_mvcc_state(db), "waits": jobs.waits()}


def _mvcc_state(db):
    """Each session's transaction and what it would see now, and every row still holding versions."""
    manager, catalog = db.mvcc, db.catalog
    sessions, views = {}, {}
    for name in SESSIONS:
        txn = db.connections[name].txn
        if txn is None:
            view = manager.snapshot()
        elif txn.snapshot is not None:
            view = txn.snapshot  # REPEATABLE READ, after its first statement
        else:
            view = manager.snapshot(txn)
        views[name] = view
        sessions[name] = {
            "xid": txn.xid if txn else None,
            "isolation": txn.isolation if txn else None,
            "savepoints": [{"name": sp["name"], "xid": sp["xid"]} for sp in db.connections[name].savepoints],
            "snapshot": {"xmin": view.xmin, "xmax": view.xmax, "active": sorted(view.active),
                         "fixed": bool(txn and txn.snapshot is not None)},
        }
    rows = []
    for table, key, versions in catalog.unsettled_rows(VERSION_ROWS):
        shown = []
        for xmin, xmax, row in versions:
            shown.append({"xmin": xmin, "xmax": xmax, "row": [_json_value(v) for v in row],
                          "status": [manager.status(xmin), manager.status(xmax) if xmax is not None else None],
                          "seen_by": [name for name, view in views.items() if manager.visible(view, xmin, xmax)]})
        rows.append({"table": table.name, "key": list(decode(key)), "versions": shown})
    return {"sessions": sessions, "next_xid": manager.next_xid,
            "running": [{"xid": t.xid, "session": t.owner, "isolation": t.isolation} for t in manager.running.values()],
            "commit_log": sorted(catalog.clog), "versions": rows,
            "serializable": [{"xid": s.xid, "session": s.owner, "committed": s.committed,
                              "reads": [t if r is None else f"{t} (keys)" for t, r in sorted(s.reads.items())]}
                             for s in db.ssi.sx.values()],
            "dependencies": db.ssi.edges()}


def _workload(db, message, conn, pump):
    """Commit batches of rows until the server pulls the plug, reporting each commit as it lands."""
    db.execute(CRASH_TABLE)
    # Repeated demos must not grow the table toward the session's size limit.
    db.execute("DELETE FROM crash_demo WHERE id <= (SELECT max(id) FROM crash_demo) - 500")
    rows_per_batch = message["rows_per_batch"]
    (start,), = db.execute("SELECT coalesce(max(id), 0) FROM crash_demo").rows
    for batch in range(1, message["batches"] + 1):
        db.execute("BEGIN")
        values = ", ".join(f"({start + (batch - 1) * rows_per_batch + i}, {batch}, '{'x' * 40}')"
                           for i in range(1, rows_per_batch + 1))
        db.execute(f"INSERT INTO crash_demo VALUES {values}")
        db.execute("COMMIT")
        db.events.emit("demo_commit", batch=batch)
        (rows,), = db.execute("SELECT count(*) FROM crash_demo").rows
        send(conn, {"progress": True, "committed_batches": batch, "rows": rows})
    return {"ok": True}
