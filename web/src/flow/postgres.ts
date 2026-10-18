// Arcanum's plan reshaped the way PostgreSQL's EXPLAIN presents one: a filter is folded into the
// operator below it (with "Rows Removed by Filter"), projections disappear into their input, a hash
// join's build side sits under a Hash node, and every operator carries Postgres's name and detail lines
// (Index Cond, Hash Cond, Group Key, Sort Key...). Both the flow diagram and the EXPLAIN text use it.
import type { PlanNode } from "../api";

export type Family = "scan" | "index" | "join" | "aggregate" | "sort" | "set" | "output" | "hash";

export interface PgNode {
  /** The id of the Arcanum operator this stands for (synthetic Hash nodes reuse their input's id, negated). */
  key: string;
  /** Every Arcanum operator folded into this one; planner decisions and advice point at these. */
  ids: number[];
  name: string;
  family: Family;
  /** The table or index it reads, for scans. */
  relation: string | null;
  details: [string, string][];
  rows: number | undefined;
  est: number | null;
  cost: number | null;
  ms: number | undefined;
  about: string;
  children: PgNode[];
  /** Set on a subquery's root: how it is attached to its parent. */
  subplan: string | null;
}

const JOIN_WORD: Record<string, string> = { INNER: "", CROSS: "", LEFT: "Left ", RIGHT: "Right ", FULL: "Full ", SEMI: "Semi ", ANTI: "Anti " };

const ABOUT = {
  seq: "Reads every row of the table, leaf page by leaf page, and tests each against the filter.",
  index: "Descends the index B+tree to the first matching key and walks its leaves. Each match then costs a second descent into the table to fetch the row.",
  pkey: "Descends the table's own B+tree (keyed by the primary key) to the first match and walks the leaf chain while keys match.",
  indexOnly: "Every column the query needs is in the index entries (index columns plus the primary key), so rows are built from the index alone: the table is never read.",
  hashJoin: "Builds a hash table from its second input (the Hash node), then probes it once per row of the first: linear in both inputs.",
  semi: "Hashes the second input once, then keeps each row of the first that has a match (Anti: that has none). A rewritten EXISTS / IN subquery runs as this.",
  nested: "Pairs every row of one input with every row of the other and tests the condition: the only strategy without an equality to hash on.",
  hash: "Reads its whole input into an in-memory hash table keyed by the join columns, for the join above to probe.",
  hashAgg: "Keeps one hash-table entry per group and updates its aggregates as rows stream in.",
  agg: "Folds every incoming row into a single result row.",
  sort: "Collects all its input and sorts it; beyond the memory budget it writes sorted runs to disk and merges them.",
  limit: "Stops pulling rows once it has enough, so the operators below stop early too.",
  window: "Computes ranks or running totals over each row's window of peers.",
  append: "Returns the rows of each input in turn.",
  setop: "Combines its inputs through a hash table of whole rows.",
  recursive: "Runs the first input once, then feeds each round's new rows back into the second until a round adds none.",
  worktable: "Reads the rows the previous round of the recursive query produced.",
  values: "Produces the rows written in the VALUES list.",
  result: "Produces a single row computed from constants: a SELECT without FROM.",
} as const;

const strip = (text: string) => (text.startsWith("(") && text.endsWith(")") ? text : `(${text})`);

function scan(node: PlanNode): Omit<PgNode, "key" | "ids" | "rows" | "est" | "cost" | "ms" | "children" | "subplan"> | null {
  const label = node.label;
  if (node.operator === "TableScan") {
    const table = label.replace(/^TableScan /, "");
    return { name: `Seq Scan on ${table}`, family: "scan", relation: table, details: [], about: ABOUT.seq };
  }
  const m = /^(IndexScan|IndexOnlyScan) (\S+) using (.+?) \((.*)\)$/.exec(label);
  if (m) {
    const [, op, table, using, cond] = m;
    const index = using === "primary key" ? `${table}_pkey` : using!;
    const only = op === "IndexOnlyScan";
    return {
      name: `${only ? "Index Only Scan" : "Index Scan"} using ${index} on ${table}`,
      family: "index",
      relation: table!,
      details: [["Index Cond", strip(cond!)]],
      about: only ? ABOUT.indexOnly : using === "primary key" ? ABOUT.pkey : ABOUT.index,
    };
  }
  if (node.operator === "WorkTableScan") {
    return { name: `WorkTable Scan on ${label.replace(/^WorkTableScan /, "")}`, family: "scan", relation: null, details: [], about: ABOUT.worktable };
  }
  if (node.operator === "Values") return { name: 'Values Scan on "*VALUES*"', family: "scan", relation: null, details: [], about: ABOUT.values };
  if (node.operator === "OneRow") return { name: "Result", family: "output", relation: null, details: [], about: ABOUT.result };
  return null;
}

/**
 * Convert an Arcanum plan. spilled: whether a sort in this statement spilled to disk (from the
 * request's stats), for the Sort Method line.
 */
export function toPostgres(root: PlanNode, spilled = false): PgNode {
  let subplans = 0;

  const make = (node: PlanNode, fields: Omit<PgNode, "key" | "ids" | "rows" | "est" | "cost" | "ms" | "subplan">): PgNode => ({
    key: String(node.id),
    ids: [node.id],
    rows: node.rows,
    est: node.est_rows,
    cost: node.est_cost ?? null,
    ms: node.ms,
    subplan: null,
    ...fields,
  });

  const withSubplans = (pg: PgNode, node: PlanNode): PgNode => {
    for (const s of node.subplans) {
      const child = convert(s.plan);
      child.subplan = `SubPlan ${++subplans}${s.correlated ? " (runs once per row)" : ""}`;
      pg.children.push(child);
    }
    return pg;
  };

  const convert = (node: PlanNode): PgNode => {
    const kids = () => node.children.map(convert);
    switch (node.operator) {
      case "Filter": {
        // Postgres has no Filter operator: the condition belongs to the node below, which reports how
        // many rows it threw away.
        const inner = convert(node.children[0]!);
        const predicate = node.label.replace(/^Filter /, "");
        const removed = inner.rows !== undefined && node.rows !== undefined ? inner.rows - node.rows : undefined;
        inner.details.push([inner.family === "join" ? "Join Filter" : "Filter", strip(predicate)]);
        if (removed !== undefined) inner.details.push(["Rows Removed by Filter", removed.toLocaleString()]);
        inner.ids.push(node.id);
        inner.rows = node.rows;
        inner.est = node.est_rows;
        inner.cost = node.est_cost ?? inner.cost;
        inner.ms = node.ms ?? inner.ms;
        return withSubplans(inner, node);
      }
      case "Project": {
        const child = node.children[0]!;
        if (child.operator === "OneRow") {
          return withSubplans(make(node, { name: "Result", family: "output", relation: null, details: [["Output", node.label.replace(/^Project /, "")]], about: ABOUT.result, children: [] }), node);
        }
        // Projections are computed inside the node that produces the rows; Postgres shows no separate step.
        const inner = convert(child);
        inner.ids.push(node.id);
        return withSubplans(inner, node);
      }
      case "Join": {
        const [word = "", kind = "INNER", ...rest] = node.label.split(" ");
        const text = rest.join(" ").replace(/^on /, "");
        const [cond, filter] = text.split(" filter ");
        const [left, right] = kids();
        const details: [string, string][] = [];
        if (word === "HashJoin") {
          details.push(["Hash Cond", strip(cond!)]);
          if (filter) details.push(["Join Filter", strip(filter)]);
          const hash: PgNode = {
            key: `hash-${node.id}`,
            ids: [],
            name: "Hash",
            family: "hash",
            relation: null,
            details: [],
            rows: right!.rows,
            est: right!.est,
            cost: right!.cost,
            ms: right!.ms,
            about: ABOUT.hash,
            children: [right!],
            subplan: null,
          };
          const semi = kind === "SEMI" || kind === "ANTI";
          return withSubplans(make(node, { name: `Hash ${JOIN_WORD[kind] ?? ""}Join`, family: "join", relation: null, details, about: semi ? ABOUT.semi : ABOUT.hashJoin, children: [left!, hash] }), node);
        }
        const residual = rest.join(" ").replace(/^filter /, "");
        if (residual) details.push(["Join Filter", strip(residual)]);
        return withSubplans(make(node, { name: `Nested Loop${JOIN_WORD[kind] ? ` ${JOIN_WORD[kind]}Join` : ""}`.trimEnd(), family: "join", relation: null, details, about: ABOUT.nested, children: [left!, right!] }), node);
      }
      case "Aggregate": {
        const m = /^HashAggregate(?: group by (.*?))?(?: compute (.*))?$/.exec(node.label);
        const groups = m?.[1];
        const details: [string, string][] = [];
        if (groups) details.push(["Group Key", groups]);
        if (m?.[2]) details.push(["Aggregates", m[2]]);
        return withSubplans(make(node, { name: groups ? "HashAggregate" : "Aggregate", family: "aggregate", relation: null, details, about: groups ? ABOUT.hashAgg : ABOUT.agg, children: kids() }), node);
      }
      case "Sort": {
        const details: [string, string][] = [["Sort Key", node.label.replace(/^Sort /, "")]];
        if (node.rows !== undefined) details.push(["Sort Method", spilled ? "external merge (spilled to disk)" : "quicksort (in memory)"]);
        return withSubplans(make(node, { name: "Sort", family: "sort", relation: null, details, about: ABOUT.sort, children: kids() }), node);
      }
      case "Limit":
        return withSubplans(make(node, { name: "Limit", family: "output", relation: null, details: [["Limit", node.label.replace(/^Limit /, "")]], about: ABOUT.limit, children: kids() }), node);
      case "Window":
        return withSubplans(make(node, { name: "WindowAgg", family: "aggregate", relation: null, details: [["Window", node.label.replace(/^Window /, "")]], about: ABOUT.window, children: kids() }), node);
      case "Distinct":
        return withSubplans(make(node, { name: "HashAggregate", family: "aggregate", relation: null, details: [["Group Key", "every output column (DISTINCT)"]], about: ABOUT.hashAgg, children: kids() }), node);
      case "SetOp": {
        const [op = "UNION"] = node.label.split(" ");
        const all = node.label.endsWith("ALL");
        const append = make(node, { name: "Append", family: "set", relation: null, details: [], about: ABOUT.append, children: kids() });
        if (op === "UNION" && all) return withSubplans(append, node);
        append.key = `append-${node.id}`;
        append.ids = [];
        const name = op === "UNION" ? "HashAggregate" : `HashSetOp ${op.charAt(0)}${op.slice(1).toLowerCase()}${all ? " All" : ""}`;
        return withSubplans(make(node, { name, family: "set", relation: null, details: op === "UNION" ? [["Group Key", "every output column (UNION removes duplicates)"]] : [], about: ABOUT.setop, children: [append] }), node);
      }
      case "RecursiveCTE":
        return withSubplans(make(node, { name: "Recursive Union", family: "set", relation: null, details: [], about: ABOUT.recursive, children: kids() }), node);
      default: {
        const s = scan(node);
        return withSubplans(make(node, s ? { ...s, children: [] } : { name: node.operator, family: "output", relation: null, details: [], about: "", children: kids() }), node);
      }
    }
  };
  return convert(root);
}

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(2));

/** The plan as PostgreSQL's EXPLAIN (ANALYZE) prints it. */
export function explainText(root: PgNode, planningMs?: number | null, executionMs?: number | null): string {
  const lines: string[] = [];
  // Postgres's layout: a child starts with "->  " two spaces past its parent's detail lines, and
  // its own detail lines sit six columns further in.
  const visit = (node: PgNode, depth: number) => {
    const detailPad = depth === 0 ? 2 : 6 * depth + 2;
    if (node.subplan) lines.push(`${" ".repeat(6 * (depth - 1) + 2)}${node.subplan}`);
    const arrow = depth === 0 ? "" : `${" ".repeat(6 * (depth - 1) + 2)}->  `;
    const parts = [];
    if (node.cost !== null || node.est !== null) {
      parts.push(`(cost=${node.cost !== null ? fmt(Math.round(node.cost * 100) / 100) : "?"} rows=${node.est !== null ? Math.max(1, Math.round(node.est)) : "?"})`);
    }
    if (node.rows !== undefined) parts.push(`(actual${node.ms !== undefined ? ` time=${node.ms.toFixed(3)}` : ""} rows=${node.rows} loops=1)`);
    lines.push(`${arrow}${node.name}  ${parts.join(" ")}`.trimEnd());
    for (const [label, value] of node.details) lines.push(`${" ".repeat(detailPad)}${label}: ${value}`);
    node.children.forEach((c) => visit(c, depth + 1));
  };
  visit(root, 0);
  if (planningMs != null) lines.push(`Planning Time: ${planningMs.toFixed(3)} ms`);
  if (executionMs != null) lines.push(`Execution Time: ${executionMs.toFixed(3)} ms`);
  return lines.join("\n");
}

/** Every node, depth first. */
export function flatten(root: PgNode): PgNode[] {
  return [root, ...root.children.flatMap(flatten)];
}
