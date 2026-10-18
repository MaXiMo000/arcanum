// Free-play challenges. Each one is judged from what the engine really did, so it cannot be ticked by hand.
import { pagesRead, planHas, treeLevels, type CheckContext } from "./outcome";

export interface Challenge {
  id: string;
  title: string;
  goal: string;
  hint: string;
  check: (ctx: CheckContext) => boolean;
}

const ran = (ctx: CheckContext) => ctx.outcome !== null;

export const CHALLENGES: Challenge[] = [
  {
    id: "three-hops",
    title: "Three hops",
    goal: "Fetch a single row by touching at most 3 tree nodes.",
    hint: "Look a row up by its primary key.",
    check: (ctx) => ran(ctx) && ctx.outcome!.results.some((r) => r.row_total === 1) && ctx.outcome!.stats.nodes_visited > 0 && ctx.outcome!.stats.nodes_visited <= 3,
  },
  {
    id: "skyscraper",
    title: "Skyscraper",
    goal: "Grow any table's tree to 5 levels.",
    hint: "Insert a few hundred rows in one statement.",
    check: (ctx) => ctx.state?.trees.some((t) => t.kind === "table" && treeLevels(t) >= 5) ?? false,
  },
  {
    id: "chain-reaction",
    title: "Chain reaction",
    goal: "Cause 25 node splits with a single statement.",
    hint: "INSERT ... SELECT from a recursive CTE.",
    check: (ctx) => ran(ctx) && ctx.outcome!.stats.splits >= 25,
  },
  {
    id: "all-in-memory",
    title: "All in memory",
    goal: "Read 50 pages or more without a single disk read.",
    hint: "Scan a table twice.",
    check: (ctx) => ran(ctx) && ctx.outcome!.stats.cache_hits >= 50 && ctx.outcome!.stats.disk_reads === 0,
  },
  {
    id: "index-builder",
    title: "Index builder",
    goal: "Create your own index and get a query to use it.",
    hint: "CREATE INDEX, ANALYZE, then filter on that column.",
    check: (ctx) => {
      const own = ctx.state?.trees.filter((t) => t.kind === "index" && t.name !== "orders_customer") ?? [];
      return own.some((t) => planHas(ctx.outcome, `using ${t.name}`));
    },
  },
  {
    id: "index-only",
    title: "Hands off the table",
    goal: "Get a query answered by an Index Only Scan.",
    hint: "Select only columns that are in an index (plus the primary key), filtered on the index's first column.",
    check: (ctx) => planHas(ctx.outcome, "IndexOnlyScan"),
  },
  {
    id: "doctors-orders",
    title: "Doctor's orders",
    goal: "Apply an advisor fix that halves the pages a query reads.",
    hint: "Run a query that scans a big table on one column, then use the fix in the Advisor tab.",
    check: (ctx) => {
      const c = ctx.comparison;
      return c !== undefined && pagesRead(c.after.stats) * 2 <= pagesRead(c.before.stats);
    },
  },
  {
    id: "clean-bill",
    title: "Clean bill of health",
    goal: "Write a join the advisor has nothing to say against.",
    hint: "Join on an equality, filter selectively, and select only what you need.",
    check: (ctx) =>
      ctx.outcome?.results.some(
        (r) => r.plan !== null && /HashJoin/.test(JSON.stringify(r.plan)) && r.advice.every((a) => a.level === "good"),
      ) ?? false,
  },
  {
    id: "semi-join",
    title: "Decorrelated",
    goal: "Write a correlated subquery the optimizer turns into a semi or anti join.",
    hint: "WHERE EXISTS (SELECT ... WHERE inner.col = outer.col).",
    check: (ctx) => planHas(ctx.outcome, "SEMI") || planHas(ctx.outcome, "ANTI"),
  },
  {
    id: "recursion",
    title: "Recursion",
    goal: "Run a recursive CTE.",
    hint: "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 10) SELECT * FROM n;",
    check: (ctx) => planHas(ctx.outcome, "RecursiveCTE"),
  },
  {
    id: "window",
    title: "Through the window",
    goal: "Rank rows with a window function.",
    hint: "rank() OVER (PARTITION BY ... ORDER BY ...)",
    check: (ctx) => planHas(ctx.outcome, "Window"),
  },
  {
    id: "undo",
    title: "Undo",
    goal: "Change something inside a transaction, then roll it back.",
    hint: "BEGIN; ...; ROLLBACK;",
    check: (ctx) => ran(ctx) && ctx.outcome!.stats.rollbacks > 0 && /\bROLLBACK\b/i.test(ctx.outcome!.sql),
  },
  {
    id: "survivor",
    title: "Survivor",
    goal: "Pull the plug and lose no committed data.",
    hint: "The button is in the top bar.",
    check: (ctx) => ctx.crash?.consistent === true,
  },
];
