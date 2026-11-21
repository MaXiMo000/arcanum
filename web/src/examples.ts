// Example queries; each one makes a different part of the engine visible.
export const EXAMPLE_GROUPS = ["Tools", "Access paths", "Joins and grouping", "Data and types", "Transactions"] as const;

export const EXAMPLES: { label: string; group: (typeof EXAMPLE_GROUPS)[number]; hint: string; sql: string }[] = [
  {
    label: "Advisor: slow scan",
    group: "Tools",
    hint: "A full scan the advisor fixes with an index; apply it and compare",
    sql: "SELECT id, status FROM orders WHERE amount BETWEEN 100 AND 130;",
  },
  {
    label: "Doctor: typos",
    group: "Tools",
    hint: "Three mistakes; the doctor fixes them one at a time",
    sql: "SELCT name, citty FROM customer WHERE id = 3;",
  },
  {
    label: "EXPLAIN ANALYZE",
    group: "Tools",
    hint: "The plan with actual rows and time per operator",
    sql: `EXPLAIN ANALYZE SELECT c.name, o.amount
FROM customers c JOIN orders o ON o.customer_id = c.id
WHERE o.amount > 400;`,
  },
  {
    label: "Index Only Scan",
    group: "Access paths",
    hint: "Everything the query reads is in the index",
    sql: "SELECT id, customer_id FROM orders WHERE customer_id = 5;",
  },
  {
    label: "Traps",
    group: "Tools",
    hint: "= NULL and a missing join condition",
    sql: "SELECT * FROM customers WHERE city = NULL;\nSELECT count(*) FROM customers, orders;",
  },
  {
    label: "Constraints",
    group: "Data and types",
    hint: "SERIAL, CHECK, a cascading foreign key, an upsert and RETURNING",
    sql: `CREATE TABLE IF NOT EXISTS tags (id SERIAL PRIMARY KEY, name TEXT UNIQUE, uses INTEGER CHECK (uses >= 0));
CREATE TABLE IF NOT EXISTS order_tags (order_id INTEGER REFERENCES orders ON DELETE CASCADE,
  tag_id INTEGER REFERENCES tags ON DELETE CASCADE, PRIMARY KEY (order_id, tag_id));
INSERT INTO tags (name, uses) VALUES ('gift', 1), ('rush', 1)
  ON CONFLICT (name) DO UPDATE SET uses = tags.uses + 1 RETURNING id, name, uses;
INSERT INTO order_tags SELECT o.id, t.id FROM orders o, tags t WHERE o.id <= 3 ON CONFLICT DO NOTHING;
DELETE FROM tags WHERE name = 'rush' RETURNING *;`,
  },
  {
    label: "Dates & numerics",
    group: "Data and types",
    hint: "DATE, NUMERIC, date_trunc, FILTER, string_agg ORDER BY and DISTINCT ON",
    sql: `SELECT date_trunc('week', DATE '2024-03-01' + id % 30) AS week,
       count(*) FILTER (WHERE amount > 300) AS big,
       sum(amount::numeric(12, 2)) AS total,
       string_agg(DISTINCT status, ', ' ORDER BY status) AS statuses
FROM orders GROUP BY 1 ORDER BY 1 LIMIT 5;
SELECT DISTINCT ON (customer_id) customer_id, id AS biggest_order, amount
FROM orders ORDER BY customer_id, amount DESC LIMIT 5;`,
  },
  {
    label: "Arrays & JSON",
    group: "Data and types",
    hint: "array_agg, unnest with LATERAL, JSONB operators, and ROLLUP subtotals",
    sql: `SELECT c.city, array_agg(o.id ORDER BY o.amount DESC) AS orders,
       jsonb_build_object('count', count(*), 'top', max(o.amount)) AS summary
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY ROLLUP (c.city) ORDER BY c.city;
SELECT c.name, big.id, big.amount
FROM customers c
CROSS JOIN LATERAL (SELECT id, amount FROM orders o WHERE o.customer_id = c.id
                    ORDER BY amount DESC LIMIT 2) big
WHERE c.id <= 3;`,
  },
  {
    label: "Point lookup",
    group: "Access paths",
    hint: "One root-to-leaf path through the primary key",
    sql: "SELECT * FROM customers WHERE id = 7;",
  },
  {
    label: "Insert 40 rows",
    group: "Data and types",
    hint: "Watch leaves fill up and split",
    sql: `INSERT INTO customers
SELECT n, 'New customer ' || n, 'Lima'
FROM (WITH RECURSIVE s(n) AS (
        SELECT (SELECT max(id) FROM customers) + 1
        UNION ALL
        SELECT n + 1 FROM s WHERE n < (SELECT max(id) FROM customers) + 40)
      SELECT n FROM s) AS seq;`,
  },
  {
    label: "Range scan",
    group: "Access paths",
    hint: "Walks the leaf chain from the first match",
    sql: "SELECT id, name FROM customers WHERE id BETWEEN 5 AND 14;",
  },
  {
    label: "Secondary index",
    group: "Access paths",
    hint: "orders_customer index, then a lookup per match",
    sql: "SELECT id, amount, status FROM orders WHERE customer_id = 5;",
  },
  {
    label: "Join + aggregate",
    group: "Joins and grouping",
    hint: "Hash join, hash aggregate, sort",
    sql: `SELECT c.city, count(*) AS orders, round(sum(o.amount), 2) AS revenue
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY c.city
ORDER BY revenue DESC;`,
  },
  {
    label: "EXISTS → semi join",
    group: "Joins and grouping",
    hint: "The optimizer removes the per-row subquery",
    sql: `SELECT name FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'open');`,
  },
  {
    label: "Window function",
    group: "Joins and grouping",
    hint: "Ranks within each customer",
    sql: `SELECT customer_id, id, amount,
       rank() OVER (PARTITION BY customer_id ORDER BY amount DESC) AS rank
FROM orders
ORDER BY customer_id, rank;`,
  },
  {
    label: "Recursive CTE",
    group: "Joins and grouping",
    hint: "Fibonacci, one iteration at a time",
    sql: `WITH RECURSIVE fib(n, a, b) AS (
  SELECT 1, 0, 1
  UNION ALL
  SELECT n + 1, b, a + b FROM fib WHERE n < 25
)
SELECT n, a FROM fib;`,
  },
  {
    label: "Merge join",
    group: "Joins and grouping",
    hint: "Both tables are sorted by id, so they are zipped together",
    sql: `SELECT c.id, c.name, o.amount
FROM customers c JOIN orders o ON o.id = c.id;`,
  },
  {
    label: "NOT EXISTS → anti join",
    group: "Joins and grouping",
    hint: "Customers with no big order: one hash table, no subquery per row",
    sql: `SELECT name FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.amount > 450);`,
  },
  {
    label: "Backward scan",
    group: "Access paths",
    hint: "ORDER BY id DESC LIMIT reads the tree from the end, no sort",
    sql: `SELECT id, amount FROM orders ORDER BY id DESC LIMIT 5;`,
  },
  {
    label: "OR → UNION ALL",
    group: "Tools",
    hint: "Two indexed columns in an OR; the advisor rewrites it",
    sql: `SELECT id, status FROM orders WHERE customer_id = 3 OR id = 7;`,
  },
  {
    label: "ROLLUP",
    group: "Joins and grouping",
    hint: "Subtotals per city and status, plus a grand total",
    sql: `SELECT city, status, count(*) AS orders, round(sum(amount), 2) AS total
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY ROLLUP (city, status)
ORDER BY city, status;`,
  },
  {
    label: "Running total",
    group: "Joins and grouping",
    hint: "A window frame that grows row by row",
    sql: `SELECT id, amount,
       sum(amount) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running
FROM orders WHERE customer_id = 8;`,
  },
  {
    label: "LATERAL top-N",
    group: "Joins and grouping",
    hint: "Each customer's two largest orders",
    sql: `SELECT c.name, top.amount
FROM customers c CROSS JOIN LATERAL (
  SELECT amount FROM orders o WHERE o.customer_id = c.id ORDER BY amount DESC LIMIT 2
) AS top
WHERE c.id <= 4;`,
  },
  {
    label: "Upsert",
    group: "Data and types",
    hint: "INSERT that updates the row when the key exists",
    sql: `INSERT INTO customers VALUES (1, 'Customer 1 (renamed)', 'Lisbon')
ON CONFLICT (id) DO UPDATE SET name = excluded.name
RETURNING id, name;`,
  },
  {
    label: "Savepoint",
    group: "Transactions",
    hint: "Undo part of a transaction and keep the rest",
    sql: `BEGIN;
UPDATE orders SET status = 'held' WHERE id = 1;
SAVEPOINT before_delete;
DELETE FROM orders WHERE id = 2;
ROLLBACK TO SAVEPOINT before_delete;
COMMIT;
SELECT id, status FROM orders WHERE id IN (1, 2);`,
  },
  {
    label: "NULL-safe compare",
    group: "Data and types",
    hint: "IS DISTINCT FROM treats NULL as a value",
    sql: `SELECT id, city,
       city = NULL AS equals_null,
       city IS NOT DISTINCT FROM NULL AS is_null_safe
FROM customers WHERE id < 4;`,
  },
  {
    label: "Delete new rows",
    group: "Data and types",
    hint: "Leaves empty out and merge",
    sql: "DELETE FROM customers WHERE name LIKE 'New customer %';",
  },
  {
    label: "Rolled-back transaction",
    group: "Transactions",
    hint: "Changes vanish; nothing reaches the log as committed",
    sql: `BEGIN;
UPDATE orders SET status = 'cancelled' WHERE customer_id = 3;
SELECT status, count(*) FROM orders GROUP BY status;
ROLLBACK;
SELECT status, count(*) FROM orders GROUP BY status;`,
  },
];
