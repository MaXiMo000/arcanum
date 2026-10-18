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

from ..engine import Database, QueryLimits
from ..errors import ArcanumError, ConstraintError, ExecutionError, SqlError
from ..events import EventBus
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

    def start_request(self):
        self.count = 0

    def on_event(self, kind, data):
        self.count += 1
        if self.count > self.limit:
            if self.count == self.limit + 1:
                self.buffer.append({"kind": "events_truncated", "limit": self.limit})
            return
        self.seq += 1
        self.buffer.append({"seq": self.seq, "kind": kind, **{k: _json_value(v) for k, v in data.items()}})
        if len(self.buffer) >= 256 or kind in _FLUSH_NOW:
            self.flush()

    def flush(self):
        if self.buffer:
            send(self.conn, self.buffer)
            self.buffer = []


def _json_value(v):
    if isinstance(v, bytes):
        try:
            return list(decode(v))  # B+tree keys use the value codec
        except (ValueError, IndexError):
            return v.hex()
    if isinstance(v, tuple):
        return list(v)
    return v


def error_json(e):
    if isinstance(e, _USER_ERRORS):
        error = {"type": type(e).__name__, "message": getattr(e, "message", str(e))}
        if isinstance(e, SqlError) and e.line is not None:
            error.update(line=e.line, column=e.col)
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
    except Exception as e:
        pump.flush()
        send(conn, {"ready": False, "error": error_json(e)})
        return
    pump.flush()
    send(conn, {"ready": True, "recovered_frames": db.recovered_frames})
    handlers = {"query": _query, "state": _state, "workload": _workload}
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
            reply = handlers[message["op"]](db, message, conn)
        except ArcanumError as e:
            reply = {"ok": False, "error": error_json(e)}
        except Exception as e:
            reply = {"ok": False, "error": error_json(e)}
        pump.flush()
        try:
            send(conn, reply)
        except ValueError as e:  # a value JSON cannot carry
            send(conn, {"ok": False, "error": error_json(e)})


def _query(db, message, conn):
    results = db.execute_script(message["sql"], trace=True, max_rows=message["max_rows"])
    return {"ok": True, "results": [
        {"columns": r.columns, "rows": [list(row) for row in r.rows], "truncated": r.row_total > len(r.rows),
         "row_total": r.row_total, "rowcount": r.rowcount, "status": r.status, "plan": r.plan}
        for r in results]}


def _state(db, message, conn):
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
        "in_transaction": db.in_transaction}


def _workload(db, message, conn):
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
