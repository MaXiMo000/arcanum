// Example queries; each one makes a different part of the engine visible.
export const EXAMPLES: { label: string; hint: string; sql: string }[] = [
  {
    label: "Point lookup",
    hint: "One root-to-leaf path through the primary key",
    sql: "SELECT * FROM customers WHERE id = 7;",
  },
  {
    label: "Insert 40 rows",
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
    hint: "Walks the leaf chain from the first match",
    sql: "SELECT id, name FROM customers WHERE id BETWEEN 5 AND 14;",
  },
  {
    label: "Secondary index",
    hint: "orders_customer index, then a lookup per match",
    sql: "SELECT id, amount, status FROM orders WHERE customer_id = 5;",
  },
  {
    label: "Join + aggregate",
    hint: "Hash join, hash aggregate, sort",
    sql: `SELECT c.city, count(*) AS orders, round(sum(o.amount), 2) AS revenue
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY c.city
ORDER BY revenue DESC;`,
  },
  {
    label: "EXISTS → semi join",
    hint: "The optimizer removes the per-row subquery",
    sql: `SELECT name FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'open');`,
  },
  {
    label: "Window function",
    hint: "Ranks within each customer",
    sql: `SELECT customer_id, id, amount,
       rank() OVER (PARTITION BY customer_id ORDER BY amount DESC) AS rank
FROM orders
ORDER BY customer_id, rank;`,
  },
  {
    label: "Recursive CTE",
    hint: "Fibonacci, one iteration at a time",
    sql: `WITH RECURSIVE fib(n, a, b) AS (
  SELECT 1, 0, 1
  UNION ALL
  SELECT n + 1, b, a + b FROM fib WHERE n < 25
)
SELECT n, a FROM fib;`,
  },
  {
    label: "Delete new rows",
    hint: "Leaves empty out and merge",
    sql: "DELETE FROM customers WHERE id > 24;",
  },
  {
    label: "Rolled-back transaction",
    hint: "Changes vanish; nothing reaches the log as committed",
    sql: `BEGIN;
UPDATE orders SET status = 'cancelled' WHERE customer_id = 3;
SELECT status, count(*) FROM orders GROUP BY status;
ROLLBACK;
SELECT status, count(*) FROM orders GROUP BY status;`,
  },
];
