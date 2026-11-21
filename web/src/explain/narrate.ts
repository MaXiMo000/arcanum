// Turns what the engine did for a statement into plain sentences, each with the reason it matters.
import type { ConstraintNote, PlanNode } from "../api";
import { pagesRead, planNodes, plural, type Outcome } from "../learn/outcome";

export type Tone = "read" | "write" | "log" | "tree" | "plan" | "ok" | "muted";

export interface Line {
  tone: Tone;
  text: string;
  why?: string;
}

function accessPath(nodes: PlanNode[]): Line | null {
  const found = nodes.find((n) => ["IndexScan", "IndexOnlyScan", "TableScan"].includes(n.operator.replace(/Backward$/, "")));
  if (!found) return null;
  const backward = found.operator.endsWith("Backward");
  // A backward scan reads the same way, from the last key down; describe it like the forward one.
  const scan = backward ? { ...found, operator: found.operator.replace(/Backward$/, ""), label: found.label.replace(/^(\w+)Backward /, "$1 ") } : found;
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
  const merges = nodes.filter((n) => n.operator === "MergeJoin").length;
  if (merges) {
    lines.push({
      tone: "plan",
      text: `${plural(merges, "merge join")} walked two already-sorted inputs side by side.`,
      why: "When both sides arrive in join-key order, matching needs no hash table: only the rows of the current key are held.",
    });
  }
  if (nodes.some((n) => n.operator.endsWith("Backward"))) {
    lines.push({
      tone: "plan",
      text: "A scan walked its B+tree backwards, so rows came out in descending order without a Sort.",
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
  lines.push(...constraintLines(stats.constraints ?? []));
  if (stats.rollbacks) {
    lines.push({ tone: "muted", text: `${plural(stats.rollbacks, "rollback")}: the pages changed were restored as they were.` });
  }
  if (stats.checkpoints) {
    lines.push({
      tone: "log",
      text: `A checkpoint copied the logged pages into the data file and emptied the log.`,
    });
  }
  if (stats.toast_writes) {
    lines.push({
      tone: "write",
      text: `${plural(stats.toast_writes, "large value")} moved to overflow pages, compressed where that helped.`,
      why: "A row must fit in a quarter of a page; like PostgreSQL's TOAST, bigger values live in a chain of overflow pages and the row keeps a pointer.",
    });
  }
  if (stats.toast_reads) {
    lines.push({
      tone: "read",
      text: `${plural(stats.toast_reads, "large value")} read back from overflow pages.`,
      why: "Reading a row follows its pointers to the overflow pages, one more page per few kilobytes.",
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

/** What the constraints did: sequences, checks, foreign key actions and ON CONFLICT outcomes. */
function constraintLines(notes: ConstraintNote[]): Line[] {
  const lines: Line[] = [];
  for (const n of notes) {
    if (n.kind === "sequence") {
      lines.push({
        tone: "write",
        text: `The sequence of ${n.table}.${n.column} handed out ${n.first === n.last ? n.first : `${n.first} to ${n.last}`}.`,
        why: "Identity and SERIAL columns number rows from a sequence, which never hands out a value twice.",
      });
    } else if (n.kind === "constraint") {
      lines.push(
        n.constraint_kind === "check"
          ? { tone: "ok", text: `CHECK ${n.name} passed for ${plural(n.rows, "row")} of ${n.table}.` }
          : n.constraint_kind === "referenced"
            ? {
                tone: "ok",
                text: `Foreign key ${n.name} looked in ${n.table} for rows still referencing ${plural(n.rows, "removed key")}.`,
                why: "A referenced row cannot simply vanish: its foreign keys decide whether to refuse, cascade or clear.",
              }
            : {
              tone: "ok",
              text: `Foreign key ${n.name} was checked for ${plural(n.rows, "key")} of ${n.table}.`,
              why: "A referencing key must match a live row of the referenced table; each check is an index or key lookup.",
            },
      );
    } else if (n.kind === "cascade") {
      lines.push({
        tone: "write",
        text: `Foreign key ${n.constraint} ${n.action === "CASCADE" ? "deleted" : "cleared the key of"} ${plural(n.rows, "row")} in ${n.table} (ON DELETE ${n.action}).`,
        why: "Removing a referenced row applies the foreign key's action to every row that references it.",
      });
    }
  }
  const conflicts = notes.filter((n) => n.kind === "conflict");
  if (conflicts.length) {
    const count = (a: string) => conflicts.reduce((sum, c) => sum + (c.action === a ? c.rows : 0), 0);
    const parts = [
      count("update") && `updated ${plural(count("update"), "existing row")}`,
      count("nothing") && `skipped ${plural(count("nothing"), "new row")}`,
      count("skipped") && `left ${plural(count("skipped"), "row")} alone (its WHERE was false)`,
    ].filter(Boolean);
    lines.push({
      tone: "write",
      text: `ON CONFLICT ${parts.join(", ")} on ${conflicts[0]!.constraint}.`,
      why: "Each proposed row is looked up on the conflict target first; a match turns the insert into an update or a skip.",
    });
  }
  return lines;
}
