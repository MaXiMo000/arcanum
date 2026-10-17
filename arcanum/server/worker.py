"""The engine worker: one process per session, owning that session's database.

The server talks to it over a pipe (one request, one reply) and receives the
engine's events through a queue, in batches. Running each database in its
own process is what lets the server kill it mid-write to demonstrate crash
recovery, and stop a runaway query without affecting anyone else.
"""

from ..engine import Database
from ..errors import ArcanumError, SqlError
from ..events import EventBus
from ..storage import decode

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


class EventPump:
    """Collects engine events and sends them to the server in batches."""

    def __init__(self, queue, limit):
        self.queue = queue
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
            self.queue.put(self.buffer)
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
    error = {"type": type(e).__name__, "message": getattr(e, "message", str(e))}
    if isinstance(e, SqlError) and e.line is not None:
        error.update(line=e.line, column=e.col)
    return error


def worker_main(path, conn, event_queue, options):
    pump = EventPump(event_queue, options["event_limit"])
    bus = EventBus()
    bus.subscribe(pump.on_event)
    try:
        db = Database(path, events=bus, max_keys=options["max_keys"], max_pages=options["max_pages"],
                      pool_size=options["pool_size"])
        if not db.catalog.tables:
            db.execute_script(SEED_SQL)
    except Exception as e:  # report startup problems instead of dying silently
        pump.flush()
        conn.send({"ready": False, "error": error_json(e)})
        return
    pump.flush()
    conn.send({"ready": True, "recovered_frames": db.recovered_frames})
    handlers = {"query": _query, "state": _state, "workload": _workload}
    while True:
        try:
            message = conn.recv()
        except EOFError:
            return
        if message["op"] == "close":
            db.close()
            conn.send({"ok": True})
            return
        pump.start_request()
        try:
            reply = handlers[message["op"]](db, message, conn)
        except ArcanumError as e:
            reply = {"ok": False, "error": error_json(e)}
        except Exception as e:
            reply = {"ok": False, "error": {"type": "InternalError", "message": str(e)}}
        pump.flush()
        conn.send(reply)


def _query(db, message, conn):
    results = []
    for result in db.execute_script(message["sql"], trace=True):
        rows = result.rows[:message["max_rows"]]
        results.append({"columns": result.columns, "rows": [list(r) for r in rows],
                        "truncated": len(result.rows) > len(rows), "row_total": len(result.rows),
                        "rowcount": result.rowcount, "status": result.status, "plan": result.plan})
    return {"ok": True, "results": results}


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
        for index in table.indexes:
            trees.append(tree("index", index.name, index.root, [table.columns[i].name for i in index.columns] + key,
                              table.name))
    tables = [{"name": t.name, "columns": [{"name": c.name, "type": c.type} for c in t.columns],
               "rows": t.stats.rows if t.stats else None} for t in catalog.tables.values()]
    return {"ok": True, "tables": tables, "trees": trees, "pager": {
        "page_size": 4096, "page_count": pager.page_count, "pool_size": pager.pool_size,
        "cached": [{"page": p, "dirty": d} for p, d in pager.cached_pages()], "free": pager.free_pages(),
        "stats": dict(pager.stats)},
        "wal": {"committed_frames": pager.wal.committed_frames, "uncommitted": pager.wal.has_uncommitted},
        "in_transaction": db.in_transaction}


def _workload(db, message, conn):
    """Commit batches of rows until the server pulls the plug, reporting each commit as it lands."""
    db.execute(CRASH_TABLE)
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
        conn.send({"progress": True, "committed_batches": batch, "rows": rows})
    return {"ok": True}
