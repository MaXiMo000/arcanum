// Plan operators under the names PostgreSQL's EXPLAIN uses, with what each one does.
import type { PlanNode } from "../api";

export type Family = "scan" | "index" | "join" | "aggregate" | "sort" | "filter" | "set" | "output";

export interface OperatorInfo {
  /** The PostgreSQL name, e.g. "Index Only Scan" or "Hash Semi Join". */
  name: string;
  family: Family;
  /** What follows the operator in Arcanum's label: table, index, condition. */
  detail: string;
  about: string;
}

const JOIN_KIND: Record<string, string> = { INNER: "", LEFT: "Left ", RIGHT: "Right ", FULL: "Full ", SEMI: "Semi ", ANTI: "Anti ", CROSS: "" };

export function operatorInfo(node: PlanNode): OperatorInfo {
  const [word = "", ...rest] = node.label.split(" ");
  const detail = rest.join(" ");
  switch (node.operator) {
    case "TableScan":
      return { name: "Seq Scan", family: "scan", detail, about: "Reads every row of the table, leaf page by leaf page, in key order." };
    case "IndexScan":
      return {
        name: "Index Scan",
        family: "index",
        detail,
        about: detail.includes("using primary key")
          ? "Descends the table's own B+tree to the first matching key, then walks the leaf chain while keys match."
          : "Descends the index to the first match and walks its leaves; each match then costs a second descent into the table to fetch the row.",
      };
    case "IndexOnlyScan":
      return {
        name: "Index Only Scan",
        family: "index",
        detail,
        about: "Every column the query needs is inside the index entries, so rows are built from the index alone. The table is never read.",
      };
    case "Filter":
      return { name: "Filter", family: "filter", detail, about: "Tests each incoming row against the condition and passes on the ones that hold." };
    case "Project":
      return { name: "Result", family: "output", detail, about: "Computes the output columns of each row." };
    case "Join": {
      const kind = rest[0] ?? "INNER";
      const condition = rest.slice(1).join(" ");
      if (word === "HashJoin") {
        return {
          name: `Hash ${JOIN_KIND[kind] ?? ""}Join`,
          family: "join",
          detail: condition,
          about:
            kind === "SEMI" || kind === "ANTI"
              ? `Hashes the right input once, then keeps each left row that ${kind === "SEMI" ? "has" : "has no"} match. This is what a rewritten EXISTS / IN subquery runs as.`
              : "Builds a hash table on one input's join keys, then probes it once per row of the other: linear in both inputs.",
        };
      }
      return {
        name: kind === "CROSS" || !condition ? "Nested Loop" : `Nested Loop ${JOIN_KIND[kind] ?? ""}Join`.trim(),
        family: "join",
        detail: condition,
        about: "Pairs every row of one input with every row of the other and tests the condition: the only choice without an equality to hash on.",
      };
    }
    case "Aggregate":
      return detail.startsWith("group by")
        ? { name: "HashAggregate", family: "aggregate", detail, about: "Keeps one hash-table entry per group and updates its aggregates as rows stream in." }
        : { name: "Aggregate", family: "aggregate", detail, about: "Folds every incoming row into a single result row." };
    case "Window":
      return { name: "WindowAgg", family: "aggregate", detail, about: "Sorts rows into their partitions, then computes ranks or running totals over each window." };
    case "Sort":
      return { name: "Sort", family: "sort", detail, about: "Collects its input and sorts it; past the memory budget it writes sorted runs to disk and merges them." };
    case "Limit":
      return { name: "Limit", family: "output", detail, about: "Stops pulling rows once it has enough, so the operators below stop early too." };
    case "Distinct":
      return { name: "HashAggregate (distinct)", family: "aggregate", detail, about: "Remembers each row it has passed in a hash set and drops repeats." };
    case "SetOp":
      return {
        name: node.label === "UNION ALL" ? "Append" : `HashSetOp ${word.charAt(0)}${word.slice(1).toLowerCase()}`,
        family: "set",
        detail: node.label.includes("ALL") ? "keeps duplicates" : "removes duplicates",
        about: node.label === "UNION ALL" ? "Returns the rows of the first input, then of the second." : "Combines its two inputs through a hash table of rows.",
      };
    case "RecursiveCTE":
      return { name: "Recursive Union", family: "set", detail, about: "Runs the anchor once, then feeds each round's new rows back into the recursive part until a round adds none." };
    case "WorkTableScan":
      return { name: "WorkTable Scan", family: "scan", detail, about: "Reads the rows the previous round of the recursive query produced." };
    case "Values":
      return { name: "Values Scan", family: "scan", detail, about: "Produces the rows written in the VALUES list." };
    case "OneRow":
      return { name: "Result", family: "output", detail: "one empty row", about: "A single row with no columns: the input of a SELECT without FROM." };
    default:
      return { name: node.operator, family: "output", detail, about: "" };
  }
}
