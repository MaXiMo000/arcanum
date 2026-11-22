// Every plan a query has had in this tab, so a plan can be compared with the one before it: after
// CREATE INDEX, ANALYZE or more rows, the optimizer may choose differently.
import { useCallback, useState } from "react";
import type { PlanNode } from "../api";
import type { Outcome } from "../learn/outcome";
import { pagesRead } from "../learn/outcome";
import { readJson, tab, writeStorage } from "../storage";

const KEY = "arcanum.plans.v1";
const QUERIES = 30; // queries remembered, the least recently run dropped first
const RUNS = 12; // runs remembered per query

export interface PlanRun {
  at: number;
  /** The plan as EXPLAIN prints it, one operator per line, without measured rows or times. */
  lines: string[];
  cost: number | null;
  rows: number | null;
  ms: number | null;
  /** Pages read, when the run was this one statement (pages are counted per run). */
  pages: number | null;
}

export type PlanHistory = Record<string, PlanRun[]>;

/** One key per query: EXPLAIN [ANALYZE] and spacing, case of keywords and a final semicolon do not matter. */
export function queryKey(statement: string): string {
  return statement
    .replace(/^\s*EXPLAIN\s+(ANALYZE\s+)?/i, "")
    .replace(/;\s*$/, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function planLines(node: PlanNode, depth = 0): string[] {
  return [
    `${"  ".repeat(depth)}${depth ? "-> " : ""}${node.label}`,
    ...node.children.flatMap((c) => planLines(c, depth + 1)),
    ...node.subplans.flatMap((s) => planLines(s.plan, depth + 1)),
  ];
}

export const samePlan = (a: PlanRun, b: PlanRun) => a.lines.join("\n") === b.lines.join("\n");

/** A line diff of two plans (longest common subsequence): each line kept, removed or added. */
export function diffLines(before: string[], after: string[]): { kind: "same" | "removed" | "added"; text: string }[] {
  const n = before.length;
  const m = after.length;
  const common = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      common[i]![j] = before[i] === after[j] ? common[i + 1]![j + 1]! + 1 : Math.max(common[i + 1]![j]!, common[i]![j + 1]!);
  const out: { kind: "same" | "removed" | "added"; text: string }[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && before[i] === after[j]) {
      out.push({ kind: "same", text: before[i++]! });
      j++;
    } else if (j < m && (i === n || common[i]![j + 1]! >= common[i + 1]![j]!)) out.push({ kind: "added", text: after[j++]! });
    else out.push({ kind: "removed", text: before[i++]! });
  }
  return out;
}

function load(): PlanHistory {
  const value = readJson(tab, KEY);
  return value && typeof value === "object" && !Array.isArray(value) ? (value as PlanHistory) : {};
}

export function usePlanHistory() {
  const [history, setHistory] = useState<PlanHistory>(load);

  /** Remember the plan of every planned statement of a run. */
  const record = useCallback((outcome: Outcome) => {
    const planned = outcome.results.filter((r) => r.plan);
    if (!planned.length) return;
    setHistory((old) => {
      const next = { ...old };
      for (const result of planned) {
        const key = queryKey(result.statement);
        const run: PlanRun = {
          at: Date.now(),
          lines: planLines(result.plan!),
          cost: result.plan!.est_cost ?? null,
          rows: result.plan!.rows ?? result.row_total ?? null,
          ms: result.execution_ms ?? null,
          pages: outcome.results.length === 1 ? pagesRead(outcome.stats) : null,
        };
        delete next[key]; // re-inserted last: the most recently run query
        next[key] = [...(old[key] ?? []), run].slice(-RUNS);
      }
      const keys = Object.keys(next);
      for (const key of keys.slice(0, Math.max(0, keys.length - QUERIES))) delete next[key];
      writeStorage(tab, KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  return { history, record };
}
