// Turns what the engine did for a statement into plain sentences, each with the reason it matters.
import type { PlanNode } from "../api";
import { pagesRead, planNodes, plural, type Outcome } from "../learn/outcome";

export type Tone = "read" | "write" | "log" | "tree" | "plan" | "ok" | "muted";

export interface Line {
  tone: Tone;
  text: string;
  why?: string;
}

function accessPath(nodes: PlanNode[]): Line | null {
  const scan = nodes.find((n) => ["IndexScan", "IndexOnlyScan", "TableScan"].includes(n.operator));
  if (!scan) return null;
  const actual = scan.rows ?? 0;
  const estimate = scan.est_rows;
  const guess =
    estimate !== null
      ? ` The optimizer expected about ${Math.round(estimate).toLocaleString()} ${Math.round(estimate) === 1 ? "row" : "rows"} and got ${actual.toLocaleString()}.`
      : "";
  if (scan.operator === "IndexOnlyScan") {
    return {
      tone: "plan",
      text: `It answered from the index alone (Index Only Scan): ${scan.label.replace(/^IndexOnlyScan /, "")}.${guess}`,
      why: "Every column the query reads is inside the index entries, so the table itself was never opened.",
    };
  }
  if (scan.operator === "IndexScan") {
    return {
      tone: "plan",
      text: `It used an index: ${scan.label.replace(/^IndexScan /, "")}.${guess}`,
      why: "An index jumps straight to the matching keys instead of reading the whole table.",
    };
  }
  return {
    tone: "plan",
    text: `It scanned the whole table (${scan.label.replace(/^TableScan /, "")}).${guess}`,
    why: "Without a useful index, or when most rows match, reading everything in order is cheapest.",
  };
}

function rewrites(nodes: PlanNode[]): Line[] {
  const lines: Line[] = [];
  const semi = nodes.find((n) => / (SEMI|ANTI) /.test(n.label));
  if (semi) {
    lines.push({
      tone: "plan",
      text: `A subquery was rewritten into a ${semi.label.includes("ANTI") ? "anti" : "semi"} join.`,
      why: "The inner table is read once and hashed, instead of the subquery running again for every outer row.",
    });
  }
  const hashJoins = nodes.filter((n) => n.label.startsWith("HashJoin INNER") || n.label.startsWith("HashJoin LEFT")).length;
  if (hashJoins) {
    lines.push({
      tone: "plan",
      text: `${plural(hashJoins, "hash join")} matched rows by building a hash table on the smaller input.`,
    });
  }
  if (nodes.some((n) => n.operator === "Aggregate")) {
    lines.push({ tone: "plan", text: "Rows were grouped with a hash table, one entry per group." });
  }
  if (nodes.some((n) => n.operator === "Window")) {
    lines.push({ tone: "plan", text: "Window functions ranked or summed rows within their partitions." });
  }
  if (nodes.some((n) => n.operator === "RecursiveCTE")) {
    lines.push({ tone: "plan", text: "A recursive query fed each round's rows back in until no new rows appeared." });
  }
  return lines;
}

export function narrate(outcome: Outcome): Line[] {
  const { stats, results } = outcome;
  const lines: Line[] = [];
  const statuses = results.map((r) => r.status).filter(Boolean);
  const returned = results.filter((r) => r.columns.length).reduce((sum, r) => sum + r.row_total, 0);
  lines.push({
    tone: "ok",
    text:
      `${statuses.join(" · ") || "Done"} in ${stats.elapsed_ms.toFixed(stats.elapsed_ms < 10 ? 2 : 0)} ms` +
      (returned ? `, returning ${plural(returned, "row")}.` : "."),
  });

  const nodes = planNodes(outcome);
  const path = accessPath(nodes);
  if (path) lines.push(path);
  lines.push(...rewrites(nodes));

  if (stats.nodes_visited) {
    lines.push({
      tone: "tree",
      text: `It visited ${plural(stats.nodes_visited, "B+tree node")}.`,
      why: "Each node is one page; a lookup visits one node per level of the tree.",
    });
  }
  const reads = pagesRead(stats);
  if (reads) {
    lines.push({
      tone: "read",
      text: `It read ${plural(reads, "page")}: ${stats.cache_hits.toLocaleString()} from the buffer pool, ${stats.disk_reads.toLocaleString()} from disk.`,
      why: stats.disk_reads ? "Disk reads are the slow part; the buffer pool keeps recently used pages in memory." : "Every page was already in memory.",
    });
  }
  if (stats.splits || stats.merges) {
    const parts = [stats.splits && plural(stats.splits, "node split"), stats.merges && plural(stats.merges, "merge")].filter(Boolean);
    lines.push({
      tone: "tree",
      text: `The tree reshaped itself: ${parts.join(" and ")}${stats.root_grew ? ", and the root grew a level" : ""}${stats.root_shrank ? ", and the tree lost a level" : ""}.`,
      why: "Splits and merges keep every leaf at the same depth, so lookups stay equally fast.",
    });
  }
  if (stats.pages_written) {
    lines.push({
      tone: "write",
      text: `It changed ${plural(stats.pages_written, "page")} in memory${stats.pages_allocated ? `, allocating ${stats.pages_allocated} new` : ""}${stats.pages_freed ? ` and freeing ${stats.pages_freed}` : ""}.`,
    });
  }
  if (stats.log_frames) {
    lines.push({
      tone: "log",
      text: `${plural(stats.log_frames, "frame")} went to the write-ahead log${stats.commits ? `, ending in ${plural(stats.commits, "commit frame")}, then the log was fsynced` : ""}.`,
      why: stats.commits ? "Once the log is on disk the change survives a crash, even before the data file is updated." : undefined,
    });
  }
  if (stats.rollbacks) {
    lines.push({ tone: "muted", text: `${plural(stats.rollbacks, "rollback")}: the pages changed were restored as they were.` });
  }
  if (stats.checkpoints) {
    lines.push({
      tone: "log",
      text: `A checkpoint copied the logged pages into the data file and emptied the log.`,
    });
  }
  if (stats.sort_spills) {
    lines.push({ tone: "write", text: "The sort was too big for memory, so it wrote sorted runs to disk and merged them." });
  }
  if (!stats.nodes_visited && !reads && !stats.pages_written) {
    lines.push({ tone: "muted", text: "It needed no pages at all: everything was computed from the statement itself." });
  }
  return lines;
}

/** One line for the stage caption. */
export function headline(outcome: Outcome): string {
  const s = outcome.stats;
  const parts = [
    `${s.elapsed_ms.toFixed(s.elapsed_ms < 10 ? 1 : 0)} ms`,
    s.nodes_visited && plural(s.nodes_visited, "node"),
    pagesRead(s) && `${s.cache_hits} cached / ${s.disk_reads} disk`,
    s.splits && plural(s.splits, "split"),
    s.merges && plural(s.merges, "merge"),
    s.log_frames && plural(s.log_frames, "log frame"),
    s.commits && "committed",
  ].filter(Boolean);
  return parts.join(" · ");
}
