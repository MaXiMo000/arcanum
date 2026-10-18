// The guided course. Every step runs real SQL against the visitor's own database, then checks what the
// engine actually did (pages read, nodes visited, splits, log frames...) before moving on.
// Text wrapped in `backticks` is shown as code.
import { advised, explainText, findTree, pagesRead, planHas, plural, treeLevels, type CheckContext } from "./outcome";

export type InspectorTab = "explain" | "results" | "flow" | "advisor" | "pages" | "log" | "events";

export interface Step {
  id: string;
  title: string;
  body: string[];
  sql?: string;
  action?: "crash";
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
