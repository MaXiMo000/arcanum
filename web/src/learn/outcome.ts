// What happened when a statement ran, and helpers lessons and challenges use to judge it.
import type { CrashSummary, EngineState, PlanNode, QueryResult, QueryStats, Tree } from "../api";

export interface Outcome {
  id: number;
  sql: string;
  results: QueryResult[];
  stats: QueryStats;
  /** The database right after the statement (fetched once it finished). */
  state: EngineState | null;
}

export interface CheckContext {
  outcome: Outcome | null;
  crash: CrashSummary | null;
  state: EngineState | null;
  /** A fix applied from the advisor, measured before and after. */
  comparison?: { before: Outcome; after: Outcome };
  /** The outcome recorded for an earlier step of the same lesson, to compare against. */
  earlier: (stepId: string) => Outcome | undefined;
}

/** Levels in a tree: one more than the deepest node below the root. */
export function treeLevels(tree: Tree | undefined): number {
  if (!tree || !tree.nodes.length) return 0;
  const depth = new Map<number, number>([[tree.root, 1]]);
  let deepest = 1;
  for (const node of tree.nodes) {
    const d = depth.get(node.page) ?? 1;
    deepest = Math.max(deepest, d);
    for (const child of node.children) depth.set(child, d + 1);
  }
  return deepest;
}

export function findTree(state: EngineState | null, name: string): Tree | undefined {
  return state?.trees.find((t) => t.name === name);
}

/** Every plan node of every result, depth first, including subquery plans. */
export function planNodes(outcome: Outcome | null): PlanNode[] {
  const out: PlanNode[] = [];
  const visit = (node: PlanNode) => {
    out.push(node);
    node.children.forEach(visit);
    node.subplans.forEach((s) => visit(s.plan));
  };
  outcome?.results.forEach((r) => r.plan && visit(r.plan));
  return out;
}

/** Whether the advisor raised a finding with this code for the outcome. */
export const advised = (ctx: CheckContext, code: string) =>
  ctx.outcome?.results.some((r) => r.advice.some((a) => a.code === code)) ?? false;

export const planHas = (outcome: Outcome | null, text: string) => planNodes(outcome).some((n) => n.label.includes(text));

/** Text of EXPLAIN output in the outcome, one line per plan line. */
export function explainText(outcome: Outcome | null): string {
  return (
    outcome?.results
      .filter((r) => r.columns.length === 1 && r.columns[0] === "plan")
      .flatMap((r) => r.rows.map((row) => String(row[0])))
      .join("\n") ?? ""
  );
}

export const pagesRead = (stats: QueryStats) => stats.cache_hits + stats.disk_reads;

export function plural(n: number, one: string, many = `${one}s`) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
