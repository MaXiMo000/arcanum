# Arcanum

A relational database written from scratch in Python, built so you can watch it work.
No SQLite or Postgres underneath: Arcanum has its own pager, B+tree, SQL parser, binder,
cost-based optimizer, query executor, write-ahead log and crash recovery. Every internal step
emits an event, so a UI can show pages loading, nodes splitting and rows flowing through a
query plan in real time.

## Quick start

```python
from arcanum import Database

with Database("company.db") as db:
    db.execute_script("""
        CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept TEXT, salary REAL, manager_id INTEGER);
        INSERT INTO emp VALUES (1, 'Ada', 'eng', 150, NULL), (2, 'Bob', 'eng', 120, 1),
                               (3, 'Cy', 'ops', 90, 1), (4, 'Di', 'ops', 95, 3);
    """)

    # Top earner per department
    db.execute("""
        SELECT dept, name FROM (
            SELECT dept, name, rank() OVER (PARTITION BY dept ORDER BY salary DESC) AS r FROM emp
        ) AS ranked WHERE r = 1 ORDER BY dept
    """).rows
    # [('eng', 'Ada'), ('ops', 'Di')]

    # Walk the management chain
    db.execute("""
        WITH RECURSIVE chain(id, depth) AS (
            SELECT id, 0 FROM emp WHERE manager_id IS NULL
            UNION ALL
            SELECT e.id, c.depth + 1 FROM emp e JOIN chain c ON e.manager_id = c.id
        )
        SELECT depth, count(*) FROM chain GROUP BY depth ORDER BY depth
    """).rows
    # [(0, 1), (1, 2), (2, 1)]

    # Who manages someone? The correlated EXISTS becomes a single semi join.
    db.execute("CREATE INDEX emp_manager ON emp (manager_id)")
    db.execute("ANALYZE")
    sql = "SELECT name FROM emp e WHERE EXISTS (SELECT 1 FROM emp r WHERE r.manager_id = e.id) AND id < 3"
    db.execute(sql).rows
    # [('Ada',)]
    for (line,) in db.execute("EXPLAIN " + sql).rows:
        print(line)
    # Project name  (est=1)
    #   -> HashJoin SEMI on id = manager_id  (est=1)
    #     -> Filter (id < 3)  (est=2)
    #       -> TableScan emp  (est=4)
    #     -> TableScan emp  (est=4)
```

## Status

| Layer | State |
|---|---|
| Pager: 4 KB pages, LRU buffer pool, free list, savepoints | done |
| Order-preserving value encoding (keys and rows) | done |
| B+tree: insert, delete with merge/redistribute, range scans, invariant checker | done |
| SQL lexer and recursive-descent parser | done |
| Catalog stored in the database file, primary keys, unique and secondary indexes | done |
| Binder: name resolution, type checking, correlated subqueries | done |
| Executor: iterator-model operators, hash join, hash aggregate, window functions, recursive CTEs | done |
| Atomic statements, `BEGIN` / `COMMIT` / `ROLLBACK`, `EXPLAIN [ANALYZE]` | done |
| Write-ahead log, checkpoints and crash recovery | done |
| Cost-based optimizer: index scans, statistics, join ordering, subquery decorrelation | done |
| HTTP / WebSocket API: sessions, live event stream, crash demo | done |
| 3D visualizer | next |

## SQL support

- `CREATE TABLE` (with `PRIMARY KEY`, `NOT NULL`, `UNIQUE` and `DEFAULT`), `DROP TABLE`, `CREATE [UNIQUE] INDEX`, `DROP INDEX`
- `INSERT ... VALUES` / `INSERT ... SELECT`, `UPDATE`, `DELETE`
- `SELECT` with `DISTINCT`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY ... NULLS FIRST|LAST`, `LIMIT` and `OFFSET`
- `INNER`, `LEFT`, `RIGHT`, `FULL` and `CROSS` joins, with `ON` or `USING`
- Scalar, `IN`, `EXISTS`, correlated and `FROM` subqueries
- `WITH` and `WITH RECURSIVE`
- Window functions `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value` and
  every aggregate, with `PARTITION BY`, `ORDER BY` and `ROWS` / `RANGE` frames
- `UNION`, `INTERSECT` and `EXCEPT`, each with or without `ALL`
- Aggregates `count`, `sum`, `avg`, `min` and `max` (with `DISTINCT`)
- Functions `abs`, `round`, `length`, `lower`, `upper`, `trim`, `ltrim`, `rtrim`, `replace`, `substr`,
  `coalesce`, `ifnull` and `nullif`; `CASE`, `CAST`, `BETWEEN`, `LIKE`, `IN`, `IS [NOT] NULL` and `||`
- `BEGIN`, `COMMIT`, `ROLLBACK`, `EXPLAIN [ANALYZE]`, `ANALYZE [table]`

### Semantics

- Types are `INTEGER`, `REAL`, `TEXT` and `BOOLEAN`, checked before a query runs: comparing a number
  with text, or storing text in an `INTEGER` column, is an error rather than a silent conversion.
- `NULL` follows SQL's three-valued logic, sorts first in ascending order and last in descending order.
- Integer division truncates toward zero; `%` takes integers; dividing by zero and integer overflow are errors.
- `LIKE` is case-sensitive.
- Every statement is atomic, and a committed transaction survives any crash (see below).

Errors point at the exact spot:

```
>>> db.execute("SELECT * FROM users WHERE id = 'one'")
SqlError: line 1, col 30: cannot compare INTEGER with TEXT
```

## Query optimizer

The binder's plan is correct but naive; the optimizer rewrites it into a cheaper equivalent:

1. **Decorrelation.** A correlated `EXISTS`, `NOT EXISTS` or `IN` in `WHERE` re-runs its subquery for
   every row. When the correlation is through equalities, it becomes a semi or anti hash join that reads
   the subquery's tables once. A correlated scalar aggregate such as
   `(SELECT count(*) FROM orders o WHERE o.customer = c.id)` becomes a `LEFT JOIN` to the grouped
   aggregate when that beats an index lookup per row; customers without orders still get `0`, not `NULL`.
2. **Predicate pushdown.** Conditions move down through joins, projections, aggregates (when they only
   test grouping keys), sorts and set operations, so rows are discarded as early as possible. Equalities
   between the two sides of an inner join become hash join keys, so `FROM a, b WHERE a.x = b.y` is a
   hash join, not a cross product.
3. **Access paths.** A filter on a table becomes an index scan when the primary key or an index matches
   a prefix of equalities, optionally followed by a range or an `IN (...)` list (one point lookup per
   value), and the cost model says that beats a full scan.
   Bounds can come from an enclosing query, so a correlated subquery looks its rows up through an index.
   `UPDATE` and `DELETE` find their rows the same way.
4. **Join ordering.** A cluster of inner joins is re-planned by dynamic programming over subsets of
   tables (the System R approach), choosing the order and each hash join's build side by estimated cost.

`ANALYZE` collects the statistics behind the estimates: row counts, distinct values, NULL counts,
most common values and equi-depth histograms. `EXPLAIN` shows the estimated rows of every operator and
`EXPLAIN ANALYZE` adds the real counts and timings. `Database(path, optimize=False)` runs the
unoptimized plans, for comparison.

## Durability and crash recovery

Arcanum keeps a write-ahead log next to the database file (`<name>-wal`):

1. **Commit.** The new image of every page the transaction changed is appended to the log, followed by
   a commit frame, and the log is fsynced. `COMMIT` returns only after that, and the data file is not
   touched.
2. **Big transactions.** When the buffer pool is full, changed pages are written to the log early,
   without a commit frame, so they count only if the transaction commits. `ROLLBACK` cuts them off again.
3. **Checkpoint.** Once the log grows past a threshold (and on close), committed pages are copied into
   the data file, the data file is fsynced, and only then is the log emptied.
4. **Recovery.** Opening a database scans the log. Every frame carries a CRC-32 chained from the frame
   before it, plus a salt that changes each time the log is emptied, so a torn write, a missing write or a
   frame left over from an older log ends the scan. Transactions that reached a commit frame are replayed;
   anything after the last commit is discarded.

A crash at any moment therefore leaves either the state before a transaction or the state after it,
never a mix.

## API server

The server gives every visitor an isolated database, seeded with sample `customers` and `orders`, and
streams what the engine does as it does it.

```bash
pip install -e ".[server]"
python -m arcanum.server
```

| Endpoint | |
|---|---|
| `POST /sessions` | Create a session with its own database. Returns its `id`. |
| `POST /sessions/{id}/query` | Run SQL (`{"sql": "..."}`; several statements allowed). Each result has `columns`, `rows`, `status` and the executed `plan` as a tree with estimated and actual rows per operator. |
| `GET /sessions/{id}/state` | Every B+tree (nodes, keys, children), the buffer pool (cached and dirty pages) and the log, for drawing the database. |
| `POST /sessions/{id}/crash` | Pull the plug: kill the engine process, by default in the middle of a stream of write transactions, then restart it. The reply compares the rows acknowledged before the crash with the rows after recovery. |
| `WS /sessions/{id}/events` | Live engine events in batches: `page_read`, `page_write`, `cache_evict`, `node_visit`, `node_split`, `node_merge`, `root_grow`, `wal_append`, `commit`, `checkpoint`, `sort_spill`, `crash`, `recovery`, `recovered`. |
| `DELETE /sessions/{id}` | Close the session and delete its files. |
| `GET /health` | Liveness. |

Errors are JSON: `{"error": {"type": "SqlError", "message": "column nope does not exist", "line": 1, "column": 8}}`.

Each session's database runs in its own worker process. That is what lets the crash demo kill the
engine for real, and what keeps one visitor's runaway query from affecting anyone else: a statement that
outlives the time limit has its worker stopped and restarted, and the database recovers from its log
like after any crash. Every resource a visitor can consume is bounded: request and session rates,
sessions and WebSockets per client, statement time and concurrency, rows held in memory, sort space,
database size, returned rows and request size. See [`.env.example`](.env.example) for the settings and
[SECURITY.md](SECURITY.md) for the protections, including why a public deployment still belongs behind
a reverse proxy or CDN.

## Setup

Requires Python 3.11 or newer. The engine itself has no dependencies; the API server uses FastAPI.

```bash
pip install -e ".[dev]"
```

## Tests

```bash
python -m pytest
```

- **Differential testing against SQLite.** The same data is loaded into Arcanum and SQLite, and a corpus
  of joins, subqueries, CTEs, window functions and set operations, plus hundreds of randomly generated
  queries, must return identical results. Everything runs three times: with the optimizer off, on plain
  tables, and with secondary indexes and statistics (plus a tiny sort budget, so `ORDER BY` spills to
  disk), so every rewrite, access path and the external sort are checked.
- **Optimizer tests** check the plans themselves: point lookups read a root-to-leaf path instead of the
  whole table, index scans are chosen only when selective, and subqueries become semi and anti joins.
- **Crash testing.** A simulated file system loses power at a random file operation, then keeps a random
  subset of the unsynced writes (disks reorder them), tearing some of them. Across hundreds of crash
  points in a workload of transactions, failing statements, rollbacks and checkpoints, including crashes
  during recovery itself, the database must come back as exactly the last committed state, or that state
  plus the transaction that was committing. Another test kills a real process mid-write.
- **B+tree model testing.** Thousands of random inserts and deletes are checked against a plain
  dictionary, verifying every structural invariant (key order, key bounds, balanced depth, leaf chain).
- **API tests** run real worker processes: queries and plans, structured errors, the event stream,
  repeated mid-write crashes, runaway-query timeouts, the size cap and session limits.
- Tests for constraints, transactions, atomic statements, persistence across reopen, and error messages.

## How a query runs

```
SQL text -> lexer -> parser -> syntax tree -> binder -> optimizer -> plan -> executor -> rows
                                                 |                               |
                                         catalog (schemas, stats)    B+trees <- pager <- file
```

1. The **parser** builds a syntax tree, recording where each name appears for error messages.
2. The **binder** resolves every table and column through nested scopes, checks types and builds a
   plan in SQL's logical order: `FROM`, `WHERE`, `GROUP BY`, `HAVING`, window functions, the select list,
   `DISTINCT`, `ORDER BY`, `LIMIT`.
3. The **optimizer** rewrites that plan using statistics (see above).
4. The **executor** turns each plan node into a generator that pulls rows from its inputs on demand,
   so `LIMIT` and `EXISTS` stop work early. Equality joins use a hash join. `ORDER BY` is an external
   merge sort: each row gets one byte-string key built from the order-preserving encoding (inverted for
   `DESC`), and past a memory budget sorted runs go to temporary files and are merged back.
5. **Storage**: tables are B+trees keyed by primary key (or a hidden row id); indexes are B+trees keyed
   by the indexed values. Keys use an order-preserving byte encoding, so the tree compares raw bytes.

## Project layout

```
arcanum/
  events.py        event bus the engine reports its internal steps to
  errors.py        error types
  storage/
    pager.py       pages, buffer pool, free list, savepoints, commit and checkpoint
    wal.py         write-ahead log: frames, checksums, recovery
    files.py       file access (swappable for crash tests)
    codec.py       order-preserving encoding of value tuples
    btree.py       B+tree
  sql/
    lexer.py       tokenizer
    parser.py      recursive-descent parser
    nodes.py       syntax tree
  engine/
    database.py    Database: statements, transactions
    catalog.py     schemas, row storage and ANALYZE statistics
    binder.py      name resolution and type checking
    expr.py        typed expressions, operators and functions
    plan.py        plan nodes and EXPLAIN output
    optimizer.py   decorrelation, pushdown, access paths, join ordering, estimates
    executor.py    iterator-model operators, external sort
  server/
    app.py         FastAPI routes and WebSocket
    sessions.py    session and worker-process management, crash demo
    worker.py      the engine process behind each session
tests/
```
