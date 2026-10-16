# Arcanum

A relational database written from scratch in Python, built so you can watch it work.
No SQLite or Postgres underneath: Arcanum has its own pager, B+tree, SQL parser and,
as it grows, its own write-ahead log, crash recovery and query optimizer. Every internal
step emits an event, so a UI can show pages loading, nodes splitting and queries flowing
through their plan in real time.

## Status

| Layer | State |
|---|---|
| Pager: 4 KB pages, LRU buffer pool, free list | done |
| Order-preserving value encoding (keys and rows) | done |
| B+tree: insert, delete with merge/redistribute, range scans, invariant checker | done |
| SQL lexer and recursive-descent parser | done |
| Catalog, binder, executor | next |
| Write-ahead log and crash recovery | planned |
| Cost-based optimizer, `EXPLAIN ANALYZE` | planned |
| API with live event stream, 3D visualizer | planned |

## SQL the parser understands

- `CREATE TABLE` (with `PRIMARY KEY`, `NOT NULL`, `UNIQUE` and `DEFAULT`), `DROP TABLE`, `CREATE [UNIQUE] INDEX`, `DROP INDEX`
- `INSERT ... VALUES` / `INSERT ... SELECT`, `UPDATE`, `DELETE`
- `SELECT` with `DISTINCT`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY ... NULLS FIRST|LAST`, `LIMIT` and `OFFSET`
- `INNER`, `LEFT`, `RIGHT`, `FULL` and `CROSS` joins, with `ON` or `USING`
- Scalar, `IN`, `EXISTS`, correlated and `FROM` subqueries
- `WITH` and `WITH RECURSIVE`
- Window functions: `OVER (PARTITION BY ... ORDER BY ... ROWS|RANGE BETWEEN ...)`
- `UNION`, `INTERSECT` and `EXCEPT` (with or without `ALL`)
- `CASE`, `CAST`, `BETWEEN`, `LIKE`, `IS [NOT] NULL` and `||`
- `BEGIN`, `COMMIT`, `ROLLBACK`, `EXPLAIN [ANALYZE]`, `ANALYZE`

Errors point at the exact spot:

```
>>> parse("SELECT * FORM users")
SqlError: line 1, col 10: expected end of statement, got 'FORM'
```

## Setup

Requires Python 3.11 or newer. The engine has no runtime dependencies.

```bash
pip install -e ".[dev]"
```

## Tests

```bash
python -m pytest
```

The B+tree tests run thousands of random inserts and deletes against a plain dictionary and
check every structural invariant (key order, key bounds, balanced depth, leaf chain) along the way.

## Project layout

```
arcanum/
  events.py        event bus the engine reports its internal steps to
  storage/
    pager.py       pages on disk, buffer pool, free list
    codec.py       order-preserving encoding of value tuples
    btree.py       B+tree index
  sql/
    lexer.py       tokenizer
    parser.py      recursive-descent parser
    nodes.py       syntax tree
tests/
```
