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
| Pager: 4 KB pages, buffer pool (LRU, Clock or 2Q), free list, savepoints | done |
| Order-preserving value encoding (keys and rows) | done |
| B+tree: insert, delete with merge/redistribute, range scans, invariant checker | done |
| SQL lexer and recursive-descent parser | done |
| Catalog stored in the database file, primary keys, unique and secondary indexes | done |
| Binder: name resolution, type checking, correlated subqueries | done |
| Executor: iterator-model operators, hash join, hash aggregate, window functions, recursive CTEs | done |
| Atomic statements, `BEGIN` / `COMMIT` / `ROLLBACK`, `EXPLAIN [ANALYZE]` | done |
| MVCC: row versions, snapshots, `READ COMMITTED` / `REPEATABLE READ`, concurrent sessions | done |
| Lock waits, deadlock detection, `SERIALIZABLE` (serializable snapshot isolation) | done |
| LSM tree (memtable, SSTables, Bloom filters, leveled compaction) compared with the B+tree | done |
| Replication: a three-node Raft cluster with elections, partitions and crash recovery | done |
| Write-ahead log, checkpoints and crash recovery | done |
| Cost-based optimizer: index scans, statistics, join ordering, subquery decorrelation | done |
| Index-only scans | done |
| Query advisor and error doctor | done |
| HTTP / WebSocket API: sessions, live event stream, crash demo | done |
| Web UI: guided course, playground, plan flow, advisor, 2D and 3D views, crash demo | done |

## SQL support

- `CREATE TABLE` with `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT`, `CHECK`, `FOREIGN KEY` /
  `REFERENCES` (`ON DELETE NO ACTION | RESTRICT | CASCADE | SET NULL`), named `CONSTRAINT`s, `SERIAL`
  types and `GENERATED { ALWAYS | BY DEFAULT } AS IDENTITY`; `DROP TABLE`, `CREATE [UNIQUE] INDEX`, `DROP INDEX`
- `ALTER TABLE`:
  - `ADD` / `DROP COLUMN`, `RENAME [COLUMN]` and `RENAME TO`.
  - `ALTER COLUMN ... SET` / `DROP DEFAULT`, `SET` / `DROP NOT NULL` and `TYPE`.
  - `ADD` / `DROP CONSTRAINT` (`CHECK`, `FOREIGN KEY`, `UNIQUE`).
- `CREATE [OR REPLACE] VIEW` and `DROP VIEW`. Views are read-only and are expanded where they are used.
- `INSERT ... VALUES` / `INSERT ... SELECT` with `ON CONFLICT [(columns)] DO NOTHING | DO UPDATE SET ...
  [WHERE ...]` (the proposed row as `EXCLUDED`), `UPDATE [... FROM]`, `DELETE [... USING]`; all three with
  `RETURNING`
- `SELECT` with `DISTINCT`, `DISTINCT ON`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY ... NULLS FIRST|LAST`, `LIMIT` and `OFFSET`
- `INNER`, `LEFT`, `RIGHT`, `FULL` and `CROSS` joins, with `ON` or `USING`
- Scalar, `IN`, `EXISTS`, correlated and `FROM` subqueries; `LATERAL` subqueries and functions in `FROM`
  (`unnest`, `generate_series`, `jsonb_each`, `jsonb_array_elements`, `jsonb_object_keys`,
  `regexp_split_to_table`), with `AS t(a, b)` column names
- `GROUP BY ROLLUP (...)`, `CUBE (...)` and `GROUPING SETS (...)`, with `grouping()`
- `WITH` and `WITH RECURSIVE`
- Window functions `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value` and
  every aggregate, with `PARTITION BY`, `ORDER BY` and `ROWS` / `RANGE` frames
- `UNION`, `INTERSECT` and `EXCEPT`, each with or without `ALL`
- Aggregates `count`, `sum`, `avg`, `min`, `max`, `string_agg`, `array_agg`, `jsonb_agg`, `jsonb_object_agg`,
  `bool_and`, `bool_or` and `every`, with
  `DISTINCT`, `ORDER BY` inside the call and `FILTER (WHERE ...)`
- Dates and times:
  - Typed literals (`DATE '2024-01-31'`, `INTERVAL '7 days'`) and `x::type` casts.
  - `CURRENT_DATE`, `CURRENT_TIMESTAMP` and `now()`, fixed for the whole statement.
  - Arithmetic: date ± days, date − date, timestamp ± interval, timestamp − timestamp, interval × number.
  - `EXTRACT` / `date_part`, `date_trunc`, `age`, `to_char`, `to_date`, `to_timestamp`, `make_date`.
- Text: `length`, `lower`, `upper`, `trim`/`ltrim`/`rtrim`/`btrim`, `replace`, `substr`, `left`, `right`,
  `lpad`/`rpad`, `position`/`strpos`, `split_part`, `initcap`, `repeat`, `reverse`, `starts_with`, `concat`,
  `concat_ws`, `translate`, `chr`/`ascii` and `regexp_replace`.
- Pattern matching: `LIKE`, `ILIKE`, and the regular expression operators `~`, `~*`, `!~` and `!~*`.
- Numbers: `abs`, `round`, `trunc`, `ceil`, `floor`, `mod`, `power`, `sqrt`, `exp`, `ln`, `log`, `sign`,
  `pi` and `random`.
- Arrays:
  - Types `INTEGER[]`, `TEXT[]` and so on, written `ARRAY[...]` or `'{...}'`.
  - Subscripts `a[i]`, `= ANY (...)` and `op ALL (...)`, `@>`, `<@`, `&&` and `||`.
  - Functions `array_length`, `cardinality`, `array_append`, `array_prepend`, `array_cat`, `array_remove`,
    `array_position`, `array_to_string` and `string_to_array`.
- JSONB (`JSON` is accepted as the same type):
  - Operators `->`, `->>`, `#>`, `#>>`, `@>`, `<@`, `?`, `?|`, `?&`, `||` and `-`, and subscripts `j['key']`.
  - Functions `to_jsonb`, `jsonb_build_object`, `jsonb_build_array`, `jsonb_set`, `jsonb_typeof`,
    `jsonb_array_length`, `jsonb_extract_path[_text]`, `jsonb_strip_nulls` and `jsonb_pretty`, with
    `json_*` spellings.
- Other: `coalesce`, `ifnull`, `nullif`, `greatest` and `least`; `CASE`, `CAST`, `BETWEEN`, `IN`,
  `IS [NOT] NULL`, `IS [NOT] TRUE / FALSE / UNKNOWN`, `IS [NOT] DISTINCT FROM` and `||`.
- `SAVEPOINT`, `ROLLBACK TO SAVEPOINT` and `RELEASE SAVEPOINT`
- `BEGIN [ISOLATION LEVEL READ COMMITTED | REPEATABLE READ | SERIALIZABLE]`, `COMMIT`, `ROLLBACK`, `EXPLAIN [ANALYZE]`,
  `ANALYZE [table]`

### Semantics

- Types are `INTEGER`, `REAL`, `NUMERIC(p, s)`, `TEXT`, `BOOLEAN`, `DATE`, `TIMESTAMP`, `JSONB` and
  `INTERVAL` and one-dimensional arrays of the scalar types. They are checked before a query runs: comparing a number with text, or storing text
  in an `INTEGER` column, is an error rather than a silent conversion.
- As in PostgreSQL, a text literal takes the type it is compared with or stored into
  (`WHERE day > '2024-01-01'`). `NUMERIC` is exact decimal arithmetic. Timestamps have no time zone and
  are read as UTC.
- `NULL` follows SQL's three-valued logic. It sorts as the largest value, as in PostgreSQL: last in
  ascending order and first in descending order, unless `NULLS FIRST` or `LAST` says otherwise.
- Integer division truncates toward zero; `%` takes integers; dividing by zero and integer overflow are errors.
- `LIKE` is case-sensitive.
- Every statement is atomic, and a committed transaction survives any crash (see below).
- Constraints behave as in PostgreSQL:
  - A `CHECK` that evaluates to `NULL` passes.
  - Foreign keys are checked at the end of each statement, so rows in one `INSERT` may reference each
    other. A key with a `NULL` in it references nothing.
  - A referenced row that another open transaction is changing makes the statement wait.
  - `DROP TABLE` refuses while another table's foreign key references it, or a view uses it.
  - `ALTER TABLE` rewrites the rows when a column is added, dropped or retyped.
  - Indexes, `CHECK`s, sequences and other tables' foreign keys follow renames.
  - A change that would break a view is refused.
  - Sequences are never rolled back by a transaction. A value taken by a statement that fails is
    handed out again.

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
5. **Sort order.** Every node knows the order its rows come out in:
   - A table or index scan comes out in key order, forwards or backwards. The B+tree can be walked from
     its last key down.
   - A hash or merge join keeps its left input's order.

   What this gives:
   - A `Sort` whose input already arrives in the requested order is removed, and a scan is turned
     around when that gives the order (`ORDER BY id DESC`).
   - NULL placement is respected. An index keeps NULLs first, so `ORDER BY x` still sorts unless `x`
     cannot be NULL or the query says `NULLS FIRST`.
   - Under `LIMIT`, reading an index in order and stopping after the first rows can beat reading and
     sorting every row. The choice is costed.
   - An inner or left join whose inputs both arrive sorted on the join keys becomes a merge join: no hash
     table, only the rows of the current key held.
6. **Index-only scans.** Before choosing access paths the optimizer works out which columns each
   table must produce. An index whose entries hold all of them (an entry is the index columns plus the
   primary key) is costed without the table lookup per match, and the scan builds its rows from the
   index alone: `IndexOnlyScan` in `EXPLAIN`.

Costs are in row visits: a full scan costs one per row, an index scan a descent plus one per match (four
for a secondary index that must also descend the table), a hash join reads both inputs and a nested loop
pairs them all. Every plan node carries its estimated rows and cost.

### Why the plan looks the way it does

`Database.execute(sql, trace=True)` also returns `planner`: every decision the optimizer made, in order,
with planning and execution time. Subquery rewrites (and why a subquery stayed a per-row SubPlan),
conditions pushed below joins and aggregates, equalities turned into hash keys, every access path costed
for each table (a sequential scan, each usable index with its estimated rows and cost, and each index that
could not be used and why), the join orders considered with their costs, sorts removed and merge joins chosen, and the index-only
check. Each
decision points at the plan node it produced.

## Classroom mode

A teacher opens a classroom from the **Class** button and gets a six-character code (no 0/O or 1/I/L, so
it can be read aloud) and a join link. From then on every statement the teacher runs, from a lesson or
the playground, is posted with its status and first rows. Students who join see each one arrive live,
with the earlier ones below, and can open it in their own playground or run it on their own database.
A student who joins late gets the recent history; one who loses the connection reconnects.

Students only watch: nothing they do reaches the teacher or each other. Only the teacher's token, sent
as a Bearer header, can post or end the classroom. Classrooms live in memory and close after sitting idle.

## CSV import

`Database.import_csv(table, text, header=True)` loads CSV text. A new table gets a type per column,
inferred from every value (INTEGER, REAL, BOOLEAN, DATE, TIMESTAMP, otherwise TEXT), with header names
turned into plain identifiers; an existing table is filled by header name, or by position without a
header. The delimiter is detected, quoted fields may hold delimiters, quotes and newlines, and an empty
field is NULL. Every value is checked against its column first, so a bad one is reported by line and
column before anything is written. The import then runs as a `CREATE TABLE` (for a new table) and a single
`INSERT`, so it is traced and animated like any statement, and either every row arrives or none does.
Values become quoted SQL literals; nothing in the file is ever read as SQL.

## Query advisor and error doctor

Every traced statement (`Database.execute(sql, trace=True)`, which the API server uses) comes back with
`advice`: findings read from the final plan, the actual row count of every operator and the statement
itself. Each finding has a level (`warn`, `tip`, `good`), the plan operator it is about and, where the
remedy is clear, a fix: a statement to run first or the statement rewritten.

| Finding | Fix |
|---|---|
| A full scan keeps a small share of a large table | `CREATE INDEX` on the filtered columns, extended to cover the other columns read when that makes the query index-only. Before suggesting it, the advisor has the optimizer plan the query as if the index existed (a hypothetical index, like PostgreSQL's hypopg) and suggests it only if the optimizer would use it, with both costs |
| An index exists but the optimizer, without statistics, chose a scan | `ANALYZE` |
| Rows fetched one by one through an index for one or two missing columns | a covering index |
| Estimates far from the actual rows, or statistics out of date | `ANALYZE` |
| `= NULL` / `!= NULL` | rewritten to `IS [NOT] NULL` |
| `UNION` where `UNION ALL` may do | rewritten |
| `a = x OR b = y` where `a` and `b` each lead an index, read with a full scan | rewritten as one `SELECT` per branch joined by `UNION ALL`, each adding `(earlier branches) IS NOT TRUE` so no row repeats and NULLs are handled; each part then uses its own index |
| `CREATE INDEX` whose columns begin another index or the primary key, or that makes an existing index redundant | `DROP INDEX` (unique indexes are never suggested: they enforce a constraint) |
| Cartesian products, non-equality nested loops, correlated subqueries that still run per row, a column hidden inside an expression, leading-wildcard `LIKE`, `NOT IN (subquery)` and its NULL trap, `HAVING` without aggregates, large `OFFSET`, sorts that spill, `UPDATE` / `DELETE` without `WHERE` | explained |

A failed statement gets a diagnosis from the error doctor (`engine/doctor.py`): misspelled tables,
columns, functions, types and keywords (`SELCT`, `FORM`, `WHRE`), text in double quotes, a missing or
trailing comma, `==`, unclosed quotes and parentheses, a missing `FROM`, curly quotes pasted from a
document, a column missing from `GROUP BY`. It returns a plain explanation, the characters at fault and,
when the intent is clear, the corrected SQL. In a script, the statements before the failing one have
already run, so the server also returns what to run now: the corrected script from the failing statement on.

Across statements, `Database.index_report()` lists every index with the scans that used it since the
database was opened and what makes it redundant, if anything, and the indexes the advisor suggested,
ranked by how many statements each would have helped, with examples. Suggestions that now exist drop out.

`ANALYZE` collects the statistics behind the estimates: row counts, distinct values, NULL counts,
most common values and equi-depth histograms. `EXPLAIN` shows the estimated rows of every operator and
`EXPLAIN ANALYZE` adds the real counts and timings. `Database(path, optimize=False)` runs the
unoptimized plans, for comparison.

## Performance

Arcanum is pure Python, so it trades raw speed for being readable end to end. On a typical laptop, with a
20,000-row table and default settings:

| Operation | Time |
|---|---|
| Point lookup by primary key | 0.2 ms |
| Secondary index lookup (40 rows) | 0.7 ms |
| Range scan, 1,000 rows | 3.5 ms |
| Single-row update, committed and fsynced | 3.3 ms |
| Full scan with a filter, 20,000 rows | 85 ms |
| Hash join and GROUP BY, 20,000 x 500 rows | 105 ms |
| ORDER BY, 20,000 rows | 125 ms |
| Insert 20,000 rows in one statement | 2.3 s |

The B+tree keeps decoded nodes alongside the buffer pool, so a page is decoded once per read from disk
rather than on every visit; a node written by the tree is stored already decoded.

## Concurrency: MVCC

Concurrency control works like PostgreSQL's. A row is a chain of versions, each stamped with the
transaction that created it (`xmin`) and the one that deleted it (`xmax`); an `UPDATE` marks the old
version deleted and appends a new one. Every statement reads through a snapshot: the transactions that had
committed when it was taken, plus its own. `READ COMMITTED` (the default) takes a snapshot per statement;
`REPEATABLE READ` and `SERIALIZABLE` take one for the whole transaction.

```python
from arcanum import Database

db = Database("bank.db")
db.execute_script("CREATE TABLE account (id INTEGER PRIMARY KEY, balance INTEGER); INSERT INTO account VALUES (1, 100)")
a, b = db.connect("a"), db.connect("b")
a.execute("BEGIN")
a.execute("UPDATE account SET balance = 0 WHERE id = 1")
b.execute("SELECT balance FROM account").rows  # [(100,)]: no dirty reads, and no waiting
b.execute("UPDATE account SET balance = 50")     # LockWaitError: a holds the row
a.execute("COMMIT")
b.execute("SELECT balance FROM account").rows  # [(0,)]
```

- Readers never wait for writers, and writers never wait for readers.
- A write to a row another open transaction changed is a write-write conflict. Called directly from
  Python, as above, the statement fails with `LockWaitError` and can be retried. The server waits instead,
  as PostgreSQL does: the statement is parked and goes ahead when the holder commits or rolls back
  (under `READ COMMITTED` it then applies to the newest version of the row).
- While waiting, the server follows who waits for whom. A wait that would close a cycle is a deadlock:
  the transaction that detects it is rolled back with `DeadlockError`, and the others go on. A wait
  longer than the lock timeout fails with `LockTimeoutError`, and a waiting statement can be cancelled.
- Under `REPEATABLE READ`, changing a row that changed after the snapshot fails with PostgreSQL's
  "could not serialize access due to concurrent update", so updates are never lost.
- `SERIALIZABLE` is serializable snapshot isolation, as in PostgreSQL. The engine remembers what each
  serializable transaction read: primary key ranges for key lookups, the whole table for other scans.
  A read/write dependency forms when one transaction reads something a concurrent one changes. A
  transaction with a dependency in and one out (a pivot) is where write skew and the other snapshot
  anomalies come from. The transaction that creates a pivot is rolled back with "could not serialize
  access due to read/write dependencies among transactions", and the error lists the dependencies.
  The map draws them as they form.
- Primary key and `UNIQUE` checks consider every live version, including those of open transactions.
- Index entries can belong to any version of a row, so every index match is rechecked against the version
  the snapshot sees. Index-only scans stay index-only while a table has no unsettled rows (the role of
  PostgreSQL's visibility map) and check the table otherwise.
- Every commit and rollback prunes: versions no snapshot can see any more are removed with their index
  entries, and surviving versions are frozen. A row settles back to one frozen version as soon as nobody
  needs the others, and the commit log only keeps the ids that versions still mention.
- Transaction ids are reserved on disk in batches, so they are never reused after a crash; a database
  reopened with unsettled rows is vacuumed, discarding whatever unfinished transactions left behind.
- A schema change inside a transaction holds off other sessions until that transaction ends.
- Savepoints are subtransactions, as in PostgreSQL:
  - Each `SAVEPOINT` takes a new transaction id, and the transaction writes with its newest one.
  - `ROLLBACK TO` aborts that id. The versions it created vanish and the rows it locked are released,
    so a session waiting on one of them goes on.
  - The row versions on the map show which subtransaction wrote what.

## Large values: overflow pages (TOAST)

A row, meaning all of its versions together, must fit in a quarter of a B+tree page. When it doesn't, its
largest values move out, largest first, until it fits, the way PostgreSQL's TOAST does it:

- Each value moved out is compressed with zlib when that saves at least a tenth.
- It is written across a chain of overflow pages, and the row keeps a small pointer.
- The pointer carries a digest of the value, so versions that keep a value unchanged share it. An
  `UPDATE` of another column copies nothing.
- Overflow pages are freed as soon as no version of the row needs them: after a delete, an update,
  pruning or a rollback.
- Everything goes through the pager and the write-ahead log. A power loss never leaves a torn value,
  which a randomized crash test checks.

Index entries still have a size limit, as in PostgreSQL. The map colours overflow pages with their
table's colour, and **What happened** says when values moved out or were read back.

## B+tree versus LSM tree

The **Labs** view holds four experiments. The first runs one workload on two storage engines and counts every byte each one reads,
writes and syncs. One engine is Arcanum's own B+tree (pager, write-ahead log and checkpoints). The other
is an LSM tree, the design behind RocksDB, LevelDB and Cassandra:

- Writes go to a log and an in-memory memtable. A full memtable is written out as a sorted, immutable
  SSTable in level 0.
- Leveled compaction merges files into the next level, each level ten times larger. A file with nothing
  to merge with moves down without being rewritten.
- Each SSTable has a block index and a Bloom filter, so most lookups skip files without reading them.

Both engines run on a counting in-memory file system with the same memory budget: the B+tree's buffer
pool against the LSM tree's memtable and an equal block cache. The comparison reports the three
amplifications:

- write: bytes written per byte of data
- read: blocks read per lookup and per range scan
- space: bytes stored per byte of live data

It also charts bytes written over the workload and replays every flush, compaction and move on a
picture of the levels. With random keys the B+tree logs a whole page for almost every write, so the LSM
tree writes an order of magnitude less, while lookups cost about the same.

## Buffer pool policies

The pager's replacement policy is pluggable (`Database(..., pool_policy="lru" | "clock" | "2q")`):

- **LRU** evicts the least recently used page. One large scan pushes every hot page out.
- **Clock** approximates LRU with a reference bit per page and a sweeping hand, as PostgreSQL does.
- **2Q** admits new pages to a small FIFO queue and promotes only pages that come back, so a scan passes
  through without displacing the hot set.

The second lab runs real queries (skewed point lookups, the same with periodic full scans, a repeated
scan, or lookups through a secondary index) and records every page the engine asks for. It replays that
trace through each policy, and through Belady's optimal policy, which evicts the page used furthest in
the future: no real policy can beat it, so it shows how much room each one leaves. It charts hit rates
over the trace and against pool size.

## How queries scale

The fourth lab generates a TPC-H-style database (customer, orders and lineitem, simplified) at five
doubling sizes, from 1,000 to 16,000 line items, and runs the same queries at each: a primary-key lookup, a
secondary-index lookup, TPC-H Q1, Q3 and Q6, and a full sort. It counts the pages each plan asks for,
buffer pool hits included, so the number measures the plan rather than the cache, and fits the slope of
log(pages) against log(rows): about 0 for B+tree lookups, about 1 for scans, hash joins and sorts. The chart
shows each query's pages relative to the smallest size against the line of the data itself.

## Replication with Raft

The third lab is a cluster of three Arcanum databases kept identical by Raft. Each node has a log of SQL
statements and applies the committed ones to its own database:

- **Elections.** A follower that hears nothing from a leader for its randomized election timeout stands
  for the next term. A node grants one vote per term, and only to a candidate whose log is at least as up
  to date as its own, so a new leader always holds every committed entry.
- **Replication.** Writes go to the leader, which sends new entries in AppendEntries messages (empty ones
  are heartbeats). A follower accepts entries only where its log matches the leader's; otherwise the
  leader backs up until they agree, and the follower's conflicting entries are replaced.
- **Commitment.** An entry of the leader's term is committed once a majority store it; every node then
  applies it. A new leader commits a no-op first, which also commits what earlier leaders left behind.

The network is simulated in ticks, with message delays, so every message is drawn in flight. Isolate a
node or crash it and watch the rest elect a new leader. A leader cut off from the majority keeps
accepting writes it can never commit; reading from it, or from a lagging follower, returns stale data.
When the network heals, its log is repaired and those writes disappear. A crashed node keeps its term,
vote and log, and rebuilds its database by reapplying the log.

Statements are replicated, not their effects, so each node runs every statement itself. Statements that
read the clock or random numbers would give each node a different answer, so they are refused, as are
transaction control statements: each log entry is one autocommit statement. A randomized test injects
crashes, restarts and partitions and checks Raft's safety properties: at most one leader per term, and
committed log prefixes that never disagree.

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

## Web UI

Two modes share one screen: the database in the middle, an inspector on the right.

- **Learn**: eleven short lessons (B+trees, growth and merges, indexes, the buffer pool, the optimizer, the
  query advisor, two sessions at once, write skew and SERIALIZABLE, transactions, the log, crash recovery). Every step runs real SQL on your own database and
  checks what the engine actually did (nodes visited, pages read, splits, log frames) before moving on.
- **Play**: an editor with syntax highlighting, name completion and errors underlined, example queries,
  and challenges judged from what the engine really did. Switch to two sessions to get a second editor on
  the same database, like two psql terminals, and watch transactions see (or not see) each other.
  **Import CSV** loads a file into a new or existing table after previewing the inferred columns.
- **Row versions** on the map: every row that currently holds versions, each version's `xmin` and `xmax`
  coloured by whether that transaction is open, committed or rolled back, and which session sees which
  version. While a session waits for a lock, a lock graph shows who waits for whom; under
  `SERIALIZABLE`, the read/write dependencies between transactions appear as they form.
- **Labs**: the B+tree versus LSM tree comparison, with a scrubber over the LSM tree's levels, and the
  buffer pool policies compared on one recorded trace, a three-node Raft cluster you can step, partition
  and crash, and TPC-H-style queries charted as the data grows (see above).

The inspector explains every statement:

- **What happened**: the run as plain sentences, each with the reason it matters.
- **Plan**, three ways, shaped the way PostgreSQL presents a plan (filters folded into the scan below them
  with "Rows Removed by Filter", a hash join's build side under a Hash node, `Index Cond`, `Hash Cond`,
  `Group Key`, `Sort Key`, `_pkey` index names):
  - **Flow**: operators as cards with particles streaming up edges sized by row count; actual and
    estimated rows (flagged when tenfold apart), estimated cost, own share of the time, and advisor
    findings on the operators they concern. Click an operator for what it does and why the planner chose
    it: every alternative it costed, side by side.
  - **How it was chosen**: the planner's journey phase by phase (parse and bind, subquery rewrites,
    filter pushdown, access paths, join order, the index-only check, execution, and what could be
    better), each with the PostgreSQL step it corresponds to.
  - **EXPLAIN**: the plan printed as `EXPLAIN ANALYZE` would print it, with planning and execution time.
- **Advisor**: a health grade and the findings above. One click applies a fix and measures the statement
  before and after: pages read, tree nodes visited, time and access path. Below, the index report for the
  whole session: indexes the workload keeps asking for, and unused or redundant ones to drop.
- **Doctor**: on an error, the explanation, the change as a diff, and a button that applies it and runs.
- Results, the page map, the write-ahead log and the raw event stream.

The centre of the screen is the **engine map**: the whole storage stack at once, as a database textbook
draws it, with every page a query touches lighting up in each layer it passes through.

- **The B+tree** the query read (the map follows each query to the table or index it used): keys in
  every node, the path the lookup walked highlighted and spelled out ("root p2 → p12 → leaf p7"), nodes
  sliding into place when they split or merge, and the leaf chain.
- **The buffer pool**: its fixed set of page slots in memory, mirrored from the engine's own
  least-recently-used order. A page read from disk drops into a free slot or the least recently used
  page's; the slot that will be evicted next is marked; changed pages carry a dot until they are logged;
  more recently used pages are brighter; hit rate and dirty count update live.
- **The disk**: the data file page by page, coloured by the table or index that owns each page, and the
  write-ahead log, whose frames appear as they are appended, turn solid when the log is synced, and drain
  into the data file at each checkpoint. Connectors between the layers animate as pages move.
- **Share**: a link that opens the playground (both sessions' SQL) on a fresh database set up the way
  yours is, by replaying the statements that changed it. It all travels in the URL fragment, which
  browsers never send to a server, so nothing is stored anywhere.
- **Time travel**: step backwards and forwards through every event of the last query (each page read,
  node visit, split and log write) or play it back; the map shows the engine as it was at that moment,
  with the node the query is at, the path it took and the buffer slot it touched marked.
- **Pull the plug**: watch committed transactions stream in, the engine get killed mid-write, and recovery
  replay the log, with a check that every acknowledged row came back.
- Playback at full speed, normal speed or slow motion, light and dark themes, and a layout that adapts
  from a wide monitor (memory and disk side by side under the tree) down to phone width.

Switch to **3D** for the same tree as lit glass slabs with their keys written on their
faces, hung from curved pointers that leave each parent from the gap between the keys they separate, with
the leaves chained in order (a wide tree bends into a ring). A query's descent sends a comet down each
pointer it follows and rings each node it reaches; splits flare. Under the tree, the data file is a
platform of tiles, one per page, raised while in the buffer pool and flashing on every read and write; the
write-ahead log spirals down beside it until a checkpoint pours it into the floor. Studio lighting,
reflections and a restrained bloom keep the glow for what the engine is doing.
Pulling the plug blows the crystals apart until recovery reassembles them. Hover anything for its page and
keys; drag to orbit. The 3D view is a separate download (three.js), fetched only when it is first opened,
and falls back to 2D where WebGL is unavailable.

Run it in development (two terminals):

```bash
python -m arcanum.server
```

```bash
cd web && npm install && npm run dev
```

Then open http://localhost:5173. For production, build once and let the API server serve it from the same
origin at http://127.0.0.1:8000:

```bash
cd web && npm ci && npm run build
```

The UI has no runtime dependencies beyond React, loads nothing from other origins, and runs under a strict
Content Security Policy.

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
| `POST /sessions/{id}/query` | Run SQL (`{"sql": "...", "session": "A"}`; several statements allowed; `session` is `A` or `B`, two connections to the same database). Each result has `columns`, `rows`, `status` and the executed `plan` as a tree with estimated and actual rows per operator. |
| `GET /sessions/{id}/tickets/{ticket}` | A statement waiting for another session's lock. `query` answers `202` with `waiting` (its `ticket`, the transaction it waits for and what it needs) when a statement has to wait; fetch the ticket until it returns the results. |
| `POST /sessions/{id}/tickets/{ticket}/cancel` | Cancel a waiting statement. |
| `POST /sessions/{id}/labs/engines` | Run one workload (`keys`, `order`, `updates`, `value_bytes`, `memory_pages`) on a B+tree and an LSM tree; returns both engines' amplifications, the bytes-written curves and the LSM tree's shape after every flush and compaction. |
| `POST /sessions/{id}/labs/buffer-pool` | Record one workload's page accesses (`workload`, `pool_pages`, `rows`, `lookups`) and replay them through every replacement policy; returns hits, misses and hit-rate curves over time and against pool size. |
| `POST /sessions/{id}/labs/scaling` | Generate the TPC-H-style dataset at five sizes and run its queries at each; returns pages, rows and time per query and size, and each query's fitted growth. |
| `POST /sessions/{id}/cluster` | Drive the session's Raft cluster: `action` is `state`, `tick` (`ticks`), `write` (`sql`), `read` (`node`, `sql`), `partition` (`groups`), `heal`, `crash` / `restart` (`node`) or `reset`. Returns every node's role, term and log, the messages in flight, the statements behind the log entries and what happened; `tick` also returns one frame per tick. |
| `POST /sessions/{id}/import` | Load CSV text (`table`, `csv`, `header`) into a new table with inferred column types or into an existing one by column name; replies like `/query`. With `dry_run` it returns a preview instead: the columns and types, the row count and the first rows. |
| `POST /classrooms` | Open a classroom (`title`); returns its `code` and the teacher's `token`. |
| `GET /classrooms/{code}` | Its title, how many students are following and the recent posts. |
| `POST /classrooms/{code}/posts` | The teacher (`Authorization: Bearer <token>`) posts a statement (`sql`), its results (`status`, `columns`, the first `rows`, `row_total`) or its `error`. |
| `DELETE /classrooms/{code}` | The teacher ends the classroom; followers are told it ended. |
| `WS /classrooms/{code}/live` | Students receive the recent posts on joining, then every new one. |
| `GET /sessions/{id}/state` | Every B+tree (nodes, keys, children), the buffer pool (cached and dirty pages) and the log, for drawing the database. |
| `POST /sessions/{id}/crash` | Pull the plug: kill the engine process, by default in the middle of a stream of write transactions, then restart it. The reply compares the rows acknowledged before the crash with the rows after recovery. |
| `WS /sessions/{id}/events` | Live engine events in batches: `page_read`, `page_write`, `cache_evict`, `node_visit`, `node_split`, `node_merge`, `root_grow`, `wal_append`, `commit`, `checkpoint`, `sort_spill`, `crash`, `recovery`, `recovered`. |
| `DELETE /sessions/{id}` | Close the session and delete its files. |
| `GET /health` | Liveness. |

Errors are JSON: `{"error": {"type": "SqlError", "message": "column nope does not exist", "line": 1, "column": 8}}`.

Each session's database runs in its own worker process. That is what lets the crash demo kill the
engine for real, and what keeps one visitor's runaway query from affecting anyone else.

A slow statement cancels itself at its statement timeout, as PostgreSQL's does, with
`StatementTimeoutError`; its session and open transaction carry on. The rare one that cannot stop part
way (a single regular-expression match) outlives the time limit, so its worker is stopped and restarted,
and the database recovers from its log like after any crash.

Every resource a visitor can consume is bounded:
- request and session rates
- sessions and WebSockets per client
- statement time and concurrency
- rows held in memory and sort space
- the size of a single value (50 million characters or array elements)
- database size, returned rows and request size See [`.env.example`](.env.example) for the settings and
[SECURITY.md](SECURITY.md) for the protections, including why a public deployment still belongs behind
a reverse proxy or CDN.

## Deploy

### Render

The repository is a ready [Render Blueprint](https://render.com/docs/infrastructure-as-code): in the
Render dashboard choose **New > Blueprint**, pick this repository, and Render builds the `Dockerfile`
and starts the service from `render.yaml`. The API and the web UI are served together from one URL.

`render.yaml` sizes the limits for the free instance (512 MB): up to 8 sessions at a time, each with
its own engine process, use under 200 MB. It also enables HSTS and trusts only Render's private proxy
addresses for `X-Forwarded-For`, so per-client limits see each visitor's real address. Sessions live in
the instance's temporary storage and end when it restarts, which suits a demo.

### Any container host

```bash
docker build -t arcanum .
```

```bash
docker run -p 8000:8000 arcanum
```

The image builds the web UI, installs the server, runs as an unprivileged user and reports its health
at `/health`. It listens on `$PORT` when the platform sets one. Configure it with the variables in
[`.env.example`](.env.example); put a reverse proxy or CDN in front of a public deployment (see
[SECURITY.md](SECURITY.md)).

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
  values.py        values that are not Python scalars: intervals, JSONB, overflow pointers, array text
  storage/
    pager.py       pages, buffer pool, free list, savepoints, commit and checkpoint
    policies.py    buffer pool replacement policies: LRU, Clock, 2Q
    wal.py         write-ahead log: frames, checksums, recovery
    files.py       file access (swappable for crash tests)
    codec.py       order-preserving encoding of value tuples
    btree.py       B+tree (forward and backward scans)
    toast.py       overflow pages for large values (TOAST)
    lsm.py         LSM tree: memtable, SSTables, Bloom filters, leveled compaction
  sql/
    lexer.py       tokenizer
    parser.py      recursive-descent parser
    nodes.py       syntax tree
  engine/
    database.py    Database: statements, transactions
    catalog.py     schemas, row storage and ANALYZE statistics
    binder.py      name resolution and type checking
    expr.py        the type system, expression nodes, conversions and value-size limits
    functions.py   built-in operators and functions, and the registries calls are checked against
    csvimport.py   CSV import: type inference, checks, and the CREATE TABLE and INSERT it becomes
    datetimes.py   dates, timestamps and intervals: parsing, arithmetic, date_trunc, to_char
    containers.py  arrays and JSONB at run time, and the functions that return rows (unnest, ...)
    dml.py         INSERT, UPDATE and DELETE with constraints, ON CONFLICT and RETURNING
    ddl.py         ALTER TABLE
    plan.py        plan nodes and EXPLAIN output
    optimizer.py   decorrelation, pushdown, access paths, join ordering, sort order, merge joins,
                   index-only scans, estimates
    advisor.py     findings and fixes from a finished plan
    doctor.py      diagnoses and corrections for failed statements
    executor.py    iterator-model operators, external sort
    mvcc.py        row versions, snapshots and visibility
    ssi.py         serializable snapshot isolation (SERIALIZABLE)
  labs/
    engines.py     the B+tree versus LSM tree comparison
    buffer_pool.py replacement policies replayed on a recorded page-access trace
    scaling.py     TPC-H-style queries at doubling data sizes
  replication/
    raft.py        Raft over three databases: elections, log replication, faults, simulated network
  server/
    app.py         FastAPI routes and WebSocket
    sessions.py    session and worker-process management, crash demo
    classroom.py   classrooms: codes, the teacher's token, live posts to students
    worker.py      the engine process behind each session
web/
  src/
    App.tsx        layout, running statements, crash demo
    useSession.ts  the database session: create, resume, replace, and the log share links replay
    storage.ts     browser storage that never throws
    stage.ts       what the 2D map and the 3D view are given
    feed.ts        WebSocket event stream and playback
    layout.ts      B+tree layout
    components/    shell, inspector, tree, pages, log, events, results, crash dialog
    learn/         lessons, challenges, progress, editor, CSV import panel
    flow/          the plan flow diagram and operator names
    advisor/       advisor findings and before/after comparison
    explain/       plain-language account of a run
    vault/         the 3D view: crystal tree, page floor, log river, camera and glow
tests/
```

## Roadmap

Arcanum aims to be the clearest way to see how a database works from the inside. Next:

- **More lessons** on the newer parts of the engine: replacement policies, replication, the index report.
- **Classroom extras**: a teacher's lesson steps followed in lockstep, and questions sent back to the teacher.
