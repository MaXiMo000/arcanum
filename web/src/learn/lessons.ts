// The guided course. Every step runs real SQL against the visitor's own database, then checks what the
// engine actually did (pages read, nodes visited, splits, log frames...) before moving on.
// Text wrapped in `backticks` is shown as code.
import type { SessionName } from "../api";
import { advised, explainText, findTree, pagesRead, planHas, planNodes, plural, treeLevels, type CheckContext } from "./outcome";

export type InspectorTab = "explain" | "results" | "flow" | "advisor" | "pages" | "log" | "events";

export interface Step {
  id: string;
  title: string;
  body: string[];
  sql?: string;
  action?: "crash";
  /** The database session the SQL runs in (A unless set). */
  session?: SessionName;
  /** The error type this step is meant to end in (it passes when the SQL fails with it). */
  expectError?: string;
  /** The step passes when its SQL starts waiting for another session's lock. */
  expectWait?: boolean;
  /** Which inspector tab tells this step's story best. */
  focus?: InspectorTab;
  check?: (ctx: CheckContext) => boolean;
  /** Shown once the check passes, built from what really happened. */
  success?: (ctx: CheckContext) => string;
  /** Shown when the step ran but the check did not pass. */
  hint?: string;
}

export interface Lesson {
  id: string;
  title: string;
  summary: string;
  minutes: number;
  steps: Step[];
}

const insertCustomers = (count: number) => `INSERT INTO customers
SELECT n, 'New customer ' || n, 'Lima'
FROM (WITH RECURSIVE s(n) AS (
        SELECT (SELECT max(id) FROM customers) + 1
        UNION ALL
        SELECT n + 1 FROM s WHERE n < (SELECT max(id) FROM customers) + ${count})
      SELECT n FROM s) AS seq;`;

const rows = (ctx: CheckContext, i = 0) => ctx.outcome?.results[i]?.row_total ?? 0;
const stat = (ctx: CheckContext) => ctx.outcome!.stats;
/** How many versions the given row currently holds (0 when it has settled to a single frozen one). */
const versionsOf = (ctx: CheckContext, table: string, id: number) =>
  ctx.state?.versions.find((r) => r.table === table && r.key[0] === id)?.versions.length ?? 0;
/** A column of the first row of a result (the last result by default). */
const firstValue = (ctx: CheckContext, column: number, result?: number) => {
  const results = ctx.outcome?.results ?? [];
  return results[result ?? results.length - 1]?.rows[0]?.[column];
};

export const LESSONS: Lesson[] = [
  {
    id: "tree",
    title: "A table is a tree",
    summary: "How a B+tree finds one row among many by reading a handful of pages.",
    minutes: 3,
    steps: [
      {
        id: "look",
        title: "Every table is a B+tree",
        body: [
          "Arcanum stores the `customers` table as a B+tree keyed by `id`. Each crystal (or box, in 2D) is one 4 KB page on disk.",
          "The bottom row holds the rows themselves, in key order: those are the leaves. The nodes above hold only separator keys that say which child to go to.",
          "Hover any node to see its page number and keys. Then continue.",
        ],
        focus: "pages",
      },
      {
        id: "lookup",
        title: "Find one row",
        body: [
          "Ask for a single customer by its key. Watch the stage: the engine starts at the root and follows exactly one child per level.",
        ],
        sql: "SELECT * FROM customers WHERE id = 17;",
        focus: "explain",
        check: (ctx) => rows(ctx) === 1 && stat(ctx).nodes_visited <= Math.max(3, treeLevels(findTree(ctx.state, "customers"))),
        success: (ctx) =>
          `Found in ${plural(stat(ctx).nodes_visited, "hop")}: one page per level of the tree, instead of reading every row. ` +
          `With a million rows the tree would be only about ${Math.ceil(Math.log(1_000_000) / Math.log(170))} levels deep at real page sizes, so a lookup still takes a few page reads.`,
      },
      {
        id: "range",
        title: "Read a range",
        body: [
          "Leaves are chained left to right. A range query descends once to the first match, then walks sideways along that chain.",
        ],
        sql: "SELECT id, name FROM customers WHERE id BETWEEN 5 AND 16;",
        focus: "flow",
        check: (ctx) => rows(ctx) === 12 && planHas(ctx.outcome, "IndexScan"),
        success: (ctx) =>
          `12 rows from ${plural(stat(ctx).nodes_visited, "node")}: one descent, then the leaf chain. The plan shows an IndexScan on the primary key with a range.`,
      },
    ],
  },
  {
    id: "grow",
    title: "Watch the tree grow",
    summary: "Splits, a taller root, and merges when rows go away.",
    minutes: 4,
    steps: [
      {
        id: "insert",
        title: "Fill the leaves",
        body: [
          "This demo keeps nodes tiny (at most 4 keys) so you can see what normally happens on 4 KB pages.",
          "Insert 40 customers. When a leaf is full it splits in two and pushes a separator key up to its parent.",
        ],
        sql: insertCustomers(40),
        focus: "events",
        check: (ctx) => stat(ctx).splits > 0,
        success: (ctx) =>
          `${plural(stat(ctx).splits, "node")} split while inserting 40 rows${stat(ctx).root_grew ? ", and the root itself split, so the tree grew a level" : ""}. Splits are the only way a B+tree grows, which is why it always stays balanced: every leaf is at the same depth.`,
      },
      {
        id: "taller",
        title: "Make it taller",
        body: ["Insert 120 more and watch the levels. The tree grows upward from the root, never downward from a leaf."],
        sql: insertCustomers(120),
        check: (ctx) => treeLevels(findTree(ctx.state, "customers")) >= 4,
        success: (ctx) =>
          `The tree now has ${treeLevels(findTree(ctx.state, "customers"))} levels for ${ctx.state?.trees.find((t) => t.name === "customers")?.nodes.filter((n) => n.leaf).reduce((s, n) => s + n.keys.length, 0) ?? "its"} rows. Each new level multiplies how many rows the tree can reach.`,
        hint: "Run it once more: the tree needs a few more rows before the root splits again.",
      },
      {
        id: "shrink",
        title: "Delete and merge",
        body: ["Remove the rows you added. Leaves that get too empty merge with a neighbour or borrow keys from it, and the freed pages go on a free list for reuse."],
        sql: "DELETE FROM customers WHERE name LIKE 'New customer %';",
        focus: "events",
        check: (ctx) => stat(ctx).merges > 0,
        success: (ctx) =>
          `${plural(stat(ctx).merges, "node")} merged or rebalanced and ${plural(stat(ctx).pages_freed, "page")} went back on the free list. The tree shrank${stat(ctx).root_shrank ? " by a whole level" : ""} without ever becoming unbalanced.`,
      },
    ],
  },
  {
    id: "indexes",
    title: "Why indexes matter",
    summary: "The same query with and without an index, page by page.",
    minutes: 4,
    steps: [
      {
        id: "scan",
        title: "Search without an index",
        body: ["Find the most expensive orders. Nothing is sorted by `amount`, so the engine has no choice but to read every order."],
        sql: "SELECT id, amount FROM orders WHERE amount > 480;",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "TableScan orders"),
        success: (ctx) =>
          `${plural(pagesRead(stat(ctx)), "page")} read to find ${plural(rows(ctx), "order")}. A full scan reads the whole table however few rows match.`,
        hint: "The plan used an index: your database already has one on amount (from the playground or the advisor). Drop it with `DROP INDEX name` (the tree tabs list it) and run this step again.",
      },
      {
        id: "create",
        title: "Build an index",
        body: [
          "An index is a second B+tree, keyed by the column you search on, whose leaves point back to the rows. `ANALYZE` refreshes the statistics the optimizer uses to decide when an index is worth it.",
        ],
        sql: "CREATE INDEX IF NOT EXISTS orders_amount ON orders (amount);\nANALYZE orders;",
        check: (ctx) => Boolean(findTree(ctx.state, "orders_amount")),
        success: () => "A new tree, orders_amount, appeared beside the table: pick it in the tree tabs to see orders sorted by amount.",
      },
      {
        id: "again",
        title: "Search again",
        body: ["Same query. The optimizer now has a second way to answer it, and its cost model decides which is cheaper."],
        sql: "SELECT id, amount FROM orders WHERE amount > 480;",
        focus: "explain",
        check: (ctx) => planHas(ctx.outcome, "orders_amount"),
        success: (ctx) => {
          const before = ctx.earlier("scan");
          const now = pagesRead(stat(ctx));
          return before
            ? `${plural(now, "page")} instead of ${pagesRead(before.stats)}: the engine walked the index straight to the matching amounts. The query reads only id and amount, both inside the index entries, so it never opened the table: an Index Only Scan.`
            : `${plural(now, "page")} read through the index on amount.`;
        },
      },
    ],
  },
  {
    id: "memory",
    title: "Memory versus disk",
    summary: "The buffer pool keeps hot pages in memory so most reads never touch the disk.",
    minutes: 2,
    steps: [
      {
        id: "first",
        title: "Read everything once",
        body: [
          "Reading a page from disk is thousands of times slower than reading it from memory, so databases keep recently used pages in a buffer pool.",
          "Count the orders. Pages not already in the pool are read from disk.",
        ],
        sql: "SELECT count(*), round(sum(amount), 2) FROM orders;",
        focus: "pages",
        check: (ctx) => pagesRead(stat(ctx)) > 0,
        success: (ctx) => `${plural(stat(ctx).disk_reads, "page")} came from disk and ${plural(stat(ctx).cache_hits, "page")} from the buffer pool.`,
      },
      {
        id: "second",
        title: "Read it again",
        body: ["Run the same query again. Every page it needs is now in memory."],
        sql: "SELECT count(*), round(sum(amount), 2) FROM orders;",
        focus: "pages",
        check: (ctx) => stat(ctx).disk_reads === 0 && stat(ctx).cache_hits > 0,
        success: (ctx) =>
          `${plural(stat(ctx).cache_hits, "cache hit")}, no disk reads. The pool holds ${ctx.state?.pager.pool_size ?? "a fixed number of"} pages; when it is full, the least recently used page is evicted to make room.`,
      },
    ],
  },
  {
    id: "optimizer",
    title: "Inside the optimizer",
    summary: "How a query is rewritten and costed before it runs.",
    minutes: 4,
    steps: [
      {
        id: "semi",
        title: "A subquery becomes a join",
        body: [
          "This asks for customers with an open order. Run naively, the subquery would execute once per customer.",
          "`EXPLAIN` shows the plan the optimizer chose instead.",
        ],
        sql: `EXPLAIN SELECT name FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'open');`,
        focus: "results",
        check: (ctx) => explainText(ctx.outcome).includes("SEMI"),
        success: () =>
          "The correlated EXISTS became a semi join: the orders are read once and hashed, then each customer is checked against that hash table, instead of re-running the subquery for every customer.",
      },
      {
        id: "choice",
        title: "Same index, different choice",
        body: [
          "There is an index on `orders.customer_id`. Compare a query that matches a few orders with one that matches almost all of them.",
        ],
        sql: "EXPLAIN SELECT id, amount FROM orders WHERE customer_id = 5;\nEXPLAIN SELECT id, amount FROM orders WHERE customer_id > 2;",
        focus: "results",
        check: (ctx) => {
          const [first, second] = ctx.outcome?.results ?? [];
          const text = (r: typeof first) => r?.rows.map((row) => String(row[0])).join("\n") ?? "";
          return text(first).includes("IndexScan") && text(second).includes("TableScan");
        },
        success: () =>
          "For a few matches the index wins. For most of the table, jumping between the index and the table costs more than one straight scan, so the optimizer scans. It decides from statistics: row counts, distinct values and histograms gathered by ANALYZE.",
      },
    ],
  },
  {
    id: "joins",
    title: "How joins pair rows",
    summary: "Hash join, merge join and nested loop: three ways to match rows, and when each wins.",
    minutes: 5,
    steps: [
      {
        id: "hash",
        title: "Hash join",
        body: [
          "Match each expensive order with its customer. The engine builds a hash table from one side, keyed by the join column, then streams the other side through it: each row finds its partners with one lookup.",
          "Open the Plan tab: the HashJoin has two inputs, and each table is read exactly once.",
        ],
        sql: `SELECT c.name, o.amount
FROM customers c JOIN orders o ON o.customer_id = c.id
WHERE o.amount > 400;`,
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "HashJoin"),
        success: (ctx) =>
          `${plural(rows(ctx), "pair")} from one pass over each table. A nested loop would have compared every customer with every order.`,
        hint: "The optimizer picked another join for your data. Look at the Plan tab to see which, and why: the planner explains each choice.",
      },
      {
        id: "merge",
        title: "Merge join",
        body: [
          "Now join on the two primary keys. Both tables are B+trees keyed by `id`, so both already come out sorted by the join column.",
          "Two sorted inputs can be zipped together like two sorted lists: no hash table, no memory, one step forward at a time.",
        ],
        sql: "SELECT c.id, c.name, o.amount FROM customers c JOIN orders o ON o.id = c.id;",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "MergeJoin"),
        success: () =>
          "A MergeJoin: both scans already deliver rows in id order, so the engine walks them side by side. Sorted order is a resource the optimizer tracks; it also lets it drop a Sort for ORDER BY.",
        hint: "No merge join this time; the Plan tab shows what the optimizer did instead and the planner says why.",
      },
      {
        id: "loop",
        title: "Nested loop",
        body: [
          "A join condition without equality cannot be hashed or merged: here an order matches a customer when its amount is more than twenty times the customer's id.",
          "The only way is a nested loop: for each row of one side, test every row of the other.",
        ],
        sql: `SELECT c.name, o.amount
FROM customers c JOIN orders o ON o.amount > c.id * 20
WHERE c.id < 3;`,
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "NestedLoopJoin"),
        success: (ctx) =>
          `A NestedLoopJoin produced ${plural(rows(ctx), "pair")}. It is cheap here because the outer side has only two customers (an index found them); on two large tables it compares every row with every row.`,
      },
      {
        id: "anti",
        title: "Anti join",
        body: [
          "Which customers have no order over 450? `NOT EXISTS` reads like a subquery per customer, but the optimizer turns it into a join that keeps only the rows with no partner.",
        ],
        sql: `SELECT name FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.amount > 450);`,
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "ANTI"),
        success: (ctx) =>
          `A HashJoin ANTI returned ${plural(rows(ctx), "customer")}: the big orders are hashed once, and a customer passes only if it finds nothing in the hash table. EXISTS becomes a SEMI join the same way.`,
      },
    ],
  },
  {
    id: "grouping",
    title: "Grouping, sorting and windows",
    summary: "Hash aggregation, sorts the optimizer can skip, and window functions that keep every row.",
    minutes: 4,
    steps: [
      {
        id: "aggregate",
        title: "Group with a hash table",
        body: [
          "Total the orders per city. The engine keeps one running total per group in a hash table, so it reads the rows once in any order.",
          "`HAVING` then filters whole groups, after they are built.",
        ],
        sql: `SELECT city, count(*) AS orders, round(sum(amount), 2) AS total
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY city HAVING count(*) > 10;`,
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "HashAggregate"),
        success: (ctx) =>
          `A HashAggregate folded the orders into ${plural(rows(ctx), "group")}; the Filter above it is the HAVING clause, applied to groups, not rows.`,
      },
      {
        id: "sort",
        title: "A sort the engine must do",
        body: [
          "The five largest orders. Nothing stores orders by amount, so the engine reads them all and sorts.",
          "With a LIMIT, the sort only has to keep the best five as it goes: a top-N sort.",
        ],
        sql: "SELECT id, amount FROM orders ORDER BY amount DESC LIMIT 5;",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "Sort"),
        success: () => "A Sort over every order, cut to five by the Limit above it.",
        hint: "Your database has an index on amount (from the indexes lesson), so the engine read it in order instead of sorting. That is the next step's point.",
      },
      {
        id: "backward",
        title: "A sort the engine can skip",
        body: [
          "The five newest orders by id. The table's B+tree is already sorted by id, and its leaves link both ways, so the engine can read it backwards from the end and stop after five rows.",
        ],
        sql: "SELECT id, amount FROM orders ORDER BY id DESC LIMIT 5;",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "Backward") && !planHas(ctx.outcome, "Sort"),
        success: (ctx) =>
          `No Sort at all: a backward scan read ${plural(pagesRead(stat(ctx)), "page")} and stopped. ORDER BY on an indexed column with a LIMIT costs almost nothing.`,
      },
      {
        id: "window",
        title: "Window functions keep every row",
        body: [
          "Rank customers within their city, newest first. Unlike GROUP BY, a window function adds a value to each row instead of folding rows together.",
        ],
        sql: `SELECT name, city, rank() OVER (PARTITION BY city ORDER BY id DESC) AS newest
FROM customers ORDER BY city, newest;`,
        focus: "results",
        check: (ctx) => planHas(ctx.outcome, "Window") && rows(ctx) > 4,
        success: (ctx) =>
          `${plural(rows(ctx), "row")}, each with its rank inside its city. The Window operator sorts by city and id once, then numbers the rows of each partition as it passes.`,
      },
    ],
  },
  {
    id: "statistics",
    title: "Statistics and estimates",
    summary: "What the optimizer guesses without ANALYZE, what it knows with it, and why plans change.",
    minutes: 5,
    steps: [
      {
        id: "skew",
        title: "A lopsided table",
        body: [
          "Most visits come from Norway; one in twenty from Iceland. Create the table with an index on `country`, but do not ANALYZE it yet.",
        ],
        sql: `DROP TABLE IF EXISTS visits;
CREATE TABLE visits (id INTEGER PRIMARY KEY, country TEXT, ms INTEGER);
INSERT INTO visits SELECT n, CASE WHEN n % 20 = 0 THEN 'IS' ELSE 'NO' END, n * 7 % 900
  FROM generate_series(1, 400) AS n;
CREATE INDEX visits_country ON visits (country);`,
        check: (ctx) => Boolean(findTree(ctx.state, "visits_country")),
        success: () => "400 visits: 380 from NO, 20 from IS, and an index on country.",
      },
      {
        id: "guess",
        title: "Planning on a guess",
        body: [
          "Ask for the Norwegian visits. Without statistics the optimizer assumes an equality matches a small share of the table, so the index looks cheap.",
          "Compare the estimated rows with the real ones in the Plan tab.",
        ],
        sql: "SELECT id, ms FROM visits WHERE country = 'NO';",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "visits_country"),
        success: (ctx) => {
          const scan = planNodes(ctx.outcome).find((n) => n.label.includes("visits_country"));
          return `The optimizer expected ${Math.round(scan?.est_rows ?? 0)} rows and got ${scan?.rows ?? rows(ctx)}. Following the index for most of a table means a lookup in the table per row: more work than reading it straight through.`;
        },
        hint: "The table already has statistics, so the optimizer was not guessing. Run the first step again to rebuild it.",
      },
      {
        id: "analyze",
        title: "Measure the table",
        body: [
          "`ANALYZE` reads the table and records its row count, distinct values, NULLs, the most common values with their frequencies, and a histogram of the rest.",
        ],
        sql: "ANALYZE visits;",
        check: (ctx) => ctx.outcome?.results.some((r) => r.status === "ANALYZE") ?? false,
        success: () => "Measured. The statistics live in the catalog, inside the database file.",
      },
      {
        id: "informed",
        title: "The same query, informed",
        body: ["Run the Norwegian query again. Nothing changed but what the optimizer knows."],
        sql: "SELECT id, ms FROM visits WHERE country = 'NO';",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "TableScan visits"),
        success: () =>
          "Now it scans: the statistics say NO is 95% of the rows, so one pass over the table beats 380 index lookups. The estimate matches the real count.",
      },
      {
        id: "rare",
        title: "A rare value",
        body: ["Same index, same query shape, a rare value."],
        sql: "SELECT id, ms FROM visits WHERE country = 'IS';",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "visits_country"),
        success: () =>
          "Back to the index: IS is 5% of the rows, so jumping to them is cheaper than reading everything. The plan depends on the value, and the statistics are how the optimizer tells them apart.",
      },
    ],
  },
  {
    id: "advisor",
    title: "The query advisor",
    summary: "Let the engine critique a slow query, apply its fix, and measure the difference.",
    minutes: 5,
    steps: [
      {
        id: "setup",
        title: "A table worth tuning",
        body: [
          "Tuning only matters once a table spans many pages. This creates `visits`, 200 page views across 20 pages of a website (it adds the rows only once, and clears this lesson's indexes so you can replay it).",
        ],
        sql: `CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, page TEXT, ms INTEGER);
DROP INDEX IF EXISTS visits_page;
DROP INDEX IF EXISTS visits_page_ms;
INSERT INTO visits
SELECT n, '/page-' || (n % 20), (n * 53) % 900 + 20
FROM (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 200) SELECT n FROM s) AS seq
WHERE (SELECT count(*) FROM visits) = 0;
ANALYZE visits;`,
        focus: "events",
        check: (ctx) => (ctx.state?.tables.find((t) => t.name === "visits")?.rows ?? 0) >= 200,
        success: (ctx) => `visits is ready: ${plural(ctx.state?.tables.find((t) => t.name === "visits")?.rows ?? 0, "row")} in a tree of its own.`,
      },
      {
        id: "slow",
        title: "Ask for one page's visits",
        body: [
          "How long did visitors spend on `/page-7`? Run it, then open the **Advisor** tab: it reads the plan and the real row counts, and says what an expert would change.",
        ],
        sql: "SELECT id, ms FROM visits WHERE page = '/page-7';",
        focus: "advisor",
        check: (ctx) => advised(ctx, "missing-index"),
        success: (ctx) =>
          `A Seq Scan read all 200 rows (${plural(pagesRead(stat(ctx)), "page")}) to keep 10. The advisor proposes an index on (page, ms). The next steps build it in two stages, to show what each column buys.`,
        hint: "The advisor flags a full scan once the table spans enough pages. Did the first step add the rows?",
      },
      {
        id: "index",
        title: "Apply the fix",
        body: ["Start with an index on `page` alone. (In the Advisor tab, a fix runs with one click and is measured before and after for you.)"],
        sql: "CREATE INDEX IF NOT EXISTS visits_page ON visits (page);",
        check: (ctx) => Boolean(findTree(ctx.state, "visits_page")),
        success: () => "A new B+tree, visits_page, keyed by page and then id. Pick it in the tree tabs to see its leaves.",
      },
      {
        id: "faster",
        title: "Run it again",
        body: ["Same query. Watch the stage: one descent through the new index, then a hop into the table for each match."],
        sql: "SELECT id, ms FROM visits WHERE page = '/page-7';",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "using visits_page"),
        success: (ctx) => {
          const before = ctx.earlier("slow");
          return before
            ? `${plural(pagesRead(stat(ctx)), "page")} instead of ${pagesRead(before.stats)}. The flow shows an Index Scan, but each of the 10 rows still costs a lookup in the table to read ms: check the Advisor again.`
            : `${plural(pagesRead(stat(ctx)), "page")} through the index.`;
        },
      },
      {
        id: "covering",
        title: "Never touch the table",
        body: [
          "The advisor suggests a covering index: put `ms` in the index too, and every column the query reads sits in the index entries (the primary key `id` is always there).",
          "Then the engine runs an **Index Only Scan**, the same trick PostgreSQL uses.",
        ],
        sql: `DROP INDEX IF EXISTS visits_page;
CREATE INDEX IF NOT EXISTS visits_page_ms ON visits (page, ms);
SELECT id, ms FROM visits WHERE page = '/page-7';`,
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "IndexOnlyScan"),
        success: (ctx) => {
          const first = ctx.earlier("slow");
          return `Index Only Scan: ${plural(pagesRead(stat(ctx)), "page")}, all from visits_page_ms${first ? `, down from ${pagesRead(first.stats)} for the full scan` : ""}. Next, make a typo in the playground: the doctor will spot it and offer the corrected SQL.`;
        },
      },
    ],
  },
  {
    id: "index-design",
    title: "Designing indexes",
    summary: "Composite indexes, why column order matters, redundant indexes and OR across two indexes.",
    minutes: 5,
    steps: [
      {
        id: "composite",
        title: "One index, two columns",
        body: [
          "An index on `(customer_id, amount)` sorts entries by customer, then by amount within each customer. It can find one customer's orders above an amount with one descent.",
          "Watch the Advisor tab: it checks every new index against the ones you already have.",
        ],
        sql: "CREATE INDEX IF NOT EXISTS orders_customer_amount ON orders (customer_id, amount);",
        focus: "advisor",
        check: (ctx) => Boolean(findTree(ctx.state, "orders_customer_amount")),
        success: (ctx) =>
          advised(ctx, "redundant-index")
            ? "Created, and the advisor noticed that orders_customer, on customer_id alone, is now redundant: the new index starts with the same column and serves every lookup the old one did. Each extra index costs work on every write."
            : "Created. The advisor checks every new index against the existing ones; here it found nothing redundant.",
      },
      {
        id: "use",
        title: "Both columns at once",
        body: ["One customer's orders over 100. Both conditions fit the index, and the query reads only `amount`, which is in the index too."],
        sql: "SELECT amount FROM orders WHERE customer_id = 3 AND amount > 100;",
        focus: "flow",
        check: (ctx) => planHas(ctx.outcome, "orders_customer_amount"),
        success: () =>
          "One descent to customer 3's entries with amount over 100, and no table lookups at all: an Index Only Scan, since everything the query needs is in the index.",
      },
      {
        id: "order",
        title: "Column order matters",
        body: [
          "Now ask by amount alone. The index is sorted by customer first, so orders over 400 are scattered across every customer's section: the index cannot jump to them.",
        ],
        sql: "SELECT id, amount FROM orders WHERE amount > 400;",
        focus: "flow",
        check: (ctx) => !planHas(ctx.outcome, "orders_customer_amount"),
        success: () =>
          "The composite index is no help here. An index serves conditions on its leading columns: (customer_id, amount) answers questions about customer_id, or customer_id and amount, but not amount alone.",
      },
      {
        id: "or",
        title: "OR across two indexes",
        body: [
          "Orders of customer 3, or order 7. Each side has an index (customer_id leads ours, id is the primary key), but one index scan cannot serve both, so the engine reads everything.",
          "The advisor suggests a rewrite: apply it from the Advisor tab and compare the pages read.",
        ],
        sql: "SELECT id, status FROM orders WHERE customer_id = 3 OR id = 7;",
        focus: "advisor",
        check: (ctx) => advised(ctx, "or-indexes"),
        success: () =>
          "The advisor offers UNION ALL: one SELECT per side, each using its own index, the second excluding rows the first already returned with IS NOT TRUE (which also handles NULLs).",
        hint: "The table is small enough or the plan changed so the advisor stayed quiet. The idea still holds: each OR branch needs its own index scan.",
      },
    ],
  },
  {
    id: "isolation",
    title: "Two sessions at once",
    summary: "Row versions, snapshots, lock waits and deadlocks, the way PostgreSQL handles them.",
    minutes: 8,
    steps: [
      {
        id: "begin",
        title: "Change a row, without committing",
        body: [
          "You now have two sessions on the same database, A and B, like two psql terminals. Session A opens a transaction and moves customer 1.",
          "PostgreSQL does not overwrite the row: it marks the old version deleted by A's transaction (its `xmax`) and adds a new version created by it (`xmin`). Watch **Row versions** on the map.",
        ],
        sql: "BEGIN;\nUPDATE customers SET city = 'Rome' WHERE id = 1;",
        session: "A",
        check: (ctx) => versionsOf(ctx, "customers", 1) === 2,
        success: (ctx) => `Customer 1 now has two versions; transaction ${ctx.state?.sessions.A.xid} created the new one and is still open.`,
      },
      {
        id: "no-dirty-read",
        title: "B reads the row",
        body: ["Session B asks for customer 1. A has not committed, so B's snapshot does not include A's transaction."],
        sql: "SELECT id, name, city FROM customers WHERE id = 1;",
        session: "B",
        check: (ctx) => firstValue(ctx, 2) === "Osaka",
        success: () => "B sees Osaka, the old version. No dirty reads: a snapshot only includes transactions that had committed when it was taken. And B did not wait: readers never block writers.",
      },
      {
        id: "write-conflict",
        title: "B tries to change the same row",
        body: [
          "Two writers on one row cannot both win. PostgreSQL makes the second writer wait until the first commits or rolls back, and so does Arcanum.",
          "Watch the lock graph on the map: B waits for A's transaction.",
        ],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 1;",
        session: "B",
        expectWait: true,
        success: () => "B is waiting, queued behind A's lock on the row. Only that row is locked: B could still change any other one, and anybody can still read it.",
        hint: "This step should wait for session A. Did A's transaction from the first step end? Run the first step again.",
      },
      {
        id: "commit",
        title: "A commits",
        body: ["Committing releases A's lock. B's waiting UPDATE goes ahead at once."],
        sql: "COMMIT;",
        session: "A",
        success: () => "Committed, and B's UPDATE went ahead: in READ COMMITTED a waiting writer re-checks the row's newest version (A's) and applies its change to that.",
      },
      {
        id: "read-committed",
        title: "B reads the row",
        body: ["B runs in READ COMMITTED, PostgreSQL's default: every statement takes a fresh snapshot."],
        sql: "SELECT id, name, city FROM customers WHERE id = 1;",
        session: "B",
        check: (ctx) => firstValue(ctx, 2) === "Lima",
        success: () => "Lima: B's update landed on top of A's. Last writer wins under READ COMMITTED, which can silently overwrite a change the writer never saw. REPEATABLE READ prevents exactly that, next.",
      },
      {
        id: "repeatable",
        title: "A snapshot that holds still",
        body: ["Now B opens a REPEATABLE READ transaction. Its snapshot is taken by the first statement and kept until it ends."],
        sql: "BEGIN ISOLATION LEVEL REPEATABLE READ;\nSELECT id, name, city FROM customers WHERE id = 2;",
        session: "B",
        check: (ctx) => firstValue(ctx, 2, 1) === "Quito",
        success: () => "Customer 2 lives in Quito, as far as B's snapshot is concerned.",
      },
      {
        id: "a-commits-change",
        title: "A changes that row and commits",
        body: ["Session A runs a single statement outside a transaction, so it commits at once."],
        sql: "UPDATE customers SET city = 'Lisbon' WHERE id = 2;",
        session: "A",
        check: (ctx) => versionsOf(ctx, "customers", 2) === 2,
        success: () => "Committed, yet both versions stay: B's snapshot still needs the old one. That is why long transactions make PostgreSQL tables bloat until VACUUM can clean up.",
      },
      {
        id: "still-quito",
        title: "B reads again",
        body: ["Same query in B's REPEATABLE READ transaction."],
        sql: "SELECT id, name, city FROM customers WHERE id = 2;",
        session: "B",
        check: (ctx) => firstValue(ctx, 2) === "Quito",
        success: () => "Still Quito: the snapshot holds still, so every read in the transaction agrees with every other.",
      },
      {
        id: "serialization",
        title: "B tries to update it",
        body: [
          "B wants to change a row that changed after its snapshot. Applying it would silently overwrite A's update: a lost update.",
        ],
        sql: "UPDATE customers SET city = 'Kyiv' WHERE id = 2;",
        session: "B",
        expectError: "SerializationError",
        success: () => "PostgreSQL's answer, word for word: could not serialize access due to concurrent update. The application retries the transaction.",
        hint: "This step should fail with a serialization error. Run the previous steps in order: B needs its REPEATABLE READ snapshot from before A's change.",
      },
      {
        id: "end",
        title: "B gives up and retries later",
        body: ["Roll back. With no snapshot needing the old version any more, the row settles."],
        sql: "ROLLBACK;",
        session: "B",
        check: (ctx) => versionsOf(ctx, "customers", 2) === 0,
        success: () => "Customer 2 is back to one frozen version. Old versions live exactly as long as a snapshot needs them.",
      },
      {
        id: "deadlock-a",
        title: "A deadlock, step one",
        body: ["Both sessions open a transaction and each changes one row: A takes customer 3, B takes customer 4."],
        sql: "BEGIN;\nUPDATE customers SET city = 'Oslo' WHERE id = 3;",
        session: "A",
      },
      {
        id: "deadlock-b",
        title: "Step two",
        body: ["B takes customer 4, then reaches for customer 3, which A holds. B waits for A."],
        sql: "BEGIN;\nUPDATE customers SET city = 'Quito' WHERE id = 4;\nUPDATE customers SET city = 'Quito' WHERE id = 3;",
        session: "B",
        expectWait: true,
        success: () => "B waits for A. Fine so far: as soon as A finishes, B goes on.",
        hint: "This step should wait for A. Run the previous step first, so A holds customer 3.",
      },
      {
        id: "deadlock",
        title: "Step three: the cycle",
        body: [
          "Now A reaches for customer 4, which B holds. A would wait for B while B waits for A: neither could ever go on.",
          "PostgreSQL checks the graph of who waits for whom, finds the cycle and rolls one transaction back.",
        ],
        sql: "UPDATE customers SET city = 'Oslo' WHERE id = 4;",
        session: "A",
        expectError: "DeadlockError",
        success: () => "Deadlock detected: A's transaction was rolled back, which released customer 3, so B's waiting update went through. Applications retry the transaction that lost.",
        hint: "This step should end in a deadlock. Run the two previous steps first.",
      },
      {
        id: "deadlock-end",
        title: "B commits",
        body: ["B is the survivor. Commit it."],
        sql: "COMMIT;\nSELECT id, city FROM customers WHERE id IN (3, 4) ORDER BY id;",
        session: "B",
        check: (ctx) => firstValue(ctx, 1, 1) === "Quito",
        success: () => "Both rows are B's. Readers never waited, writers queued per row, and the one cycle was broken by rolling back a single transaction: that is how PostgreSQL keeps many sessions safe at once.",
      },
    ],
  },
  {
    id: "serializable",
    title: "Write skew and SERIALIZABLE",
    summary: "An anomaly snapshots allow, and how PostgreSQL's SERIALIZABLE tracks read/write dependencies to stop it.",
    minutes: 6,
    steps: [
      {
        id: "rule",
        title: "A rule across two rows",
        body: [
          "Customers 1 and 2 share a delivery van, so at most one of them may live in Lima. No constraint can say that; the application checks it before moving a customer.",
          "Start with both in Osaka.",
        ],
        sql: "UPDATE customers SET city = 'Osaka' WHERE id IN (1, 2);",
        session: "A",
      },
      {
        id: "rr-a",
        title: "A checks the rule",
        body: ["Session A opens a REPEATABLE READ transaction and counts how many of the two live in Lima."],
        sql: "BEGIN ISOLATION LEVEL REPEATABLE READ;\nSELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "A",
        check: (ctx) => firstValue(ctx, 0) === 0,
        success: () => "None, so A may move one of them.",
      },
      {
        id: "rr-b",
        title: "B checks too",
        body: ["At the same moment, session B runs the same check."],
        sql: "BEGIN ISOLATION LEVEL REPEATABLE READ;\nSELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "B",
        check: (ctx) => firstValue(ctx, 0) === 0,
        success: () => "None for B as well. Both checks were right when they ran.",
      },
      {
        id: "rr-a-move",
        title: "A moves customer 1",
        body: ["A moves customer 1 to Lima and commits."],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 1;\nCOMMIT;",
        session: "A",
      },
      {
        id: "rr-b-move",
        title: "B moves customer 2",
        body: ["B moves the other customer. Its snapshot still says nobody lives in Lima, and it changes a different row, so there is no write conflict."],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 2;\nCOMMIT;",
        session: "B",
        success: () => "Committed, with no error.",
      },
      {
        id: "rr-broken",
        title: "Look again",
        body: ["Count once more, now that both transactions committed."],
        sql: "SELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "A",
        check: (ctx) => firstValue(ctx, 0) === 2,
        success: () => "Two: the rule is broken, though each transaction checked it. This is write skew. Each read what the other then changed, and in no serial order could both checks have passed. REPEATABLE READ (snapshot isolation) allows it.",
      },
      {
        id: "ser-a",
        title: "Again, with SERIALIZABLE",
        body: [
          "Reset both customers and replay the race at the SERIALIZABLE level.",
          "Now the engine remembers what each transaction read, like PostgreSQL's SIREAD locks: here the keys of customers 1 and 2.",
        ],
        sql: "UPDATE customers SET city = 'Osaka' WHERE id IN (1, 2);\nBEGIN ISOLATION LEVEL SERIALIZABLE;\nSELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "A",
        check: (ctx) => firstValue(ctx, 0) === 0,
      },
      {
        id: "ser-b",
        title: "B checks",
        body: ["B opens a SERIALIZABLE transaction and runs the same check."],
        sql: "BEGIN ISOLATION LEVEL SERIALIZABLE;\nSELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "B",
        check: (ctx) => firstValue(ctx, 0) === 0,
      },
      {
        id: "ser-a-move",
        title: "A moves customer 1",
        body: ["A moves customer 1, without committing yet. Watch the map: B read that row, and A has now changed it."],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 1;",
        session: "A",
        check: (ctx) => (ctx.state?.dependencies ?? []).length === 1,
        success: () => "A read/write dependency: B read customer 1 before A changed it, so B has to come before A in any serial order. One dependency alone is harmless.",
        hint: "Run the two previous steps first, so that both transactions are open.",
      },
      {
        id: "ser-b-move",
        title: "B moves customer 2",
        body: ["B moves the other customer. But A read that row, so now A must also come before B: a cycle, which no serial order fits."],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 2;",
        session: "B",
        expectError: "SerializationError",
        success: () => "Refused, with PostgreSQL's error: could not serialize access due to read/write dependencies among transactions. A has a dependency in and one out (a pivot), so B's transaction was rolled back. The application retries it, and the retry sees customer 1 already in Lima.",
        hint: "This step should fail with a serialization error. Run the steps of this SERIALIZABLE race in order.",
      },
      {
        id: "ser-commit",
        title: "A commits",
        body: ["A is free to finish."],
        sql: "COMMIT;\nSELECT count(*) FROM customers WHERE id IN (1, 2) AND city = 'Lima';",
        session: "A",
        check: (ctx) => firstValue(ctx, 0, 1) === 1,
        success: () => "One customer in Lima: the rule holds. SERIALIZABLE costs some bookkeeping and the occasional retry, and in return every committed result is one some serial order could have produced.",
      },
    ],
  },
  {
    id: "transactions",
    title: "Transactions",
    summary: "All or nothing: changes that can be undone until they are committed.",
    minutes: 2,
    steps: [
      {
        id: "rollback",
        title: "Delete everything, then take it back",
        body: [
          "Inside `BEGIN`, changes are visible to you but not yet permanent. `ROLLBACK` restores every page the transaction touched.",
        ],
        sql: "BEGIN;\nDELETE FROM orders;\nSELECT count(*) FROM orders;\nROLLBACK;\nSELECT count(*) FROM orders;",
        focus: "results",
        check: (ctx) => {
          const results = ctx.outcome?.results ?? [];
          return results[2]?.rows[0]?.[0] === 0 && Number(results[4]?.rows[0]?.[0]) > 0;
        },
        success: (ctx) =>
          `Inside the transaction the table was empty; after ROLLBACK all ${ctx.outcome?.results[4]?.rows[0]?.[0]} orders are back. Nothing the transaction did ever reached the log as committed.`,
      },
    ],
  },
  {
    id: "wal",
    title: "The write-ahead log",
    summary: "Why a committed change survives a crash.",
    minutes: 2,
    steps: [
      {
        id: "commit",
        title: "Change one row",
        body: [
          "Before a change counts, the engine appends every page it changed to the write-ahead log, then a commit frame, and forces the log to disk with fsync.",
          "The data file itself is updated later, at a checkpoint. Watch the log river (or the Log tab).",
        ],
        sql: "UPDATE customers SET city = 'Lima' WHERE id = 3;",
        focus: "log",
        check: (ctx) => stat(ctx).log_frames > 0 && stat(ctx).commits === 1,
        success: (ctx) =>
          `${plural(stat(ctx).log_frames, "frame")} went to the log: the changed pages, then the commit frame. Only after the fsync did the UPDATE report success. If the power fails now, the log alone is enough to bring this change back.`,
      },
    ],
  },
  {
    id: "crash",
    title: "Pull the plug",
    summary: "Kill the engine mid-write and watch recovery bring back every committed row.",
    minutes: 2,
    steps: [
      {
        id: "plug",
        title: "Pull the plug",
        body: [
          "The engine will start committing batches of rows, and partway through the next batch its process will be killed. No clean shutdown, no chance to finish.",
          "A fresh engine then opens the database, replays the log and checks the result.",
        ],
        action: "crash",
        check: (ctx) => ctx.crash?.consistent === true,
        success: (ctx) =>
          `${plural(ctx.crash?.acknowledged_rows ?? 0, "row")} had been acknowledged before the crash; ${plural(ctx.crash?.rows_after_recovery ?? 0, "row")} are there after recovery. ` +
          `The interrupted batch ${ctx.crash?.in_flight_committed ? "had already reached its commit frame, so it came back whole" : "never reached its commit frame, so none of it came back"}: never half a transaction.`,
      },
    ],
  },
];
