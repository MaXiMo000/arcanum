# Arcanum

A relational database written from scratch in Python, built so you can watch it work.
No SQLite or Postgres underneath: Arcanum has its own pager, B+tree, SQL parser, binder and
query executor, write-ahead log and crash recovery, and, as it grows, its own cost-based
optimizer. Every internal step emits an event, so a UI can show pages loading, nodes
splitting and rows flowing through a query plan in real time.

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

    for (line,) in db.execute("EXPLAIN SELECT dept, avg(salary) FROM emp WHERE salary > 100 GROUP BY dept").rows:
        print(line)
    # Project dept, avg(salary) AS avg
    #   -> HashAggregate group by dept compute avg(salary)
    #     -> Filter (salary > 100)
    #       -> TableScan emp
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
| Cost-based optimizer: index scans, statistics, join ordering, subquery decorrelation | next |
| API with live event stream, 3D visualizer | planned |

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
- `BEGIN`, `COMMIT`, `ROLLBACK`, `EXPLAIN [ANALYZE]`

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

## Setup

Requires Python 3.11 or newer. The engine has no runtime dependencies.

```bash
pip install -e ".[dev]"
```

## Tests

```bash
python -m pytest
```

- **Differential testing against SQLite.** The same data is loaded into Arcanum and SQLite, and a corpus
  of joins, subqueries, CTEs, window functions and set operations, plus hundreds of randomly generated
  queries, must return identical results.
- **Crash testing.** A simulated file system loses power at a random file operation, then keeps a random
  subset of the unsynced writes (disks reorder them), tearing some of them. Across hundreds of crash
  points in a workload of transactions, failing statements, rollbacks and checkpoints, including crashes
  during recovery itself, the database must come back as exactly the last committed state, or that state
  plus the transaction that was committing. Another test kills a real process mid-write.
- **B+tree model testing.** Thousands of random inserts and deletes are checked against a plain
  dictionary, verifying every structural invariant (key order, key bounds, balanced depth, leaf chain).
- Tests for constraints, transactions, atomic statements, persistence across reopen, and error messages.

## How a query runs

```
SQL text -> lexer -> parser -> syntax tree -> binder -> query plan -> executor -> rows
                                                 |                       |
                                         catalog (schemas)     B+trees <- pager <- file
```

1. The **parser** builds a syntax tree, recording where each name appears for error messages.
2. The **binder** resolves every table and column through nested scopes, checks types and builds a
   plan in SQL's logical order: `FROM`, `WHERE`, `GROUP BY`, `HAVING`, window functions, the select list,
   `DISTINCT`, `ORDER BY`, `LIMIT`.
3. The **executor** turns each plan node into a generator that pulls rows from its inputs on demand,
   so `LIMIT` and `EXISTS` stop work early. Equality joins use a hash join.
4. **Storage**: tables are B+trees keyed by primary key (or a hidden row id); indexes are B+trees keyed
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
    catalog.py     schemas and row storage
    binder.py      name resolution and type checking
    expr.py        typed expressions, operators and functions
    plan.py        plan nodes and EXPLAIN output
    executor.py    iterator-model operators
tests/
```
