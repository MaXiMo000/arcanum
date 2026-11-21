import type { Value } from "./api";

export function formatValue(v: Value): string {
  if (v === null) return "NULL";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(12)));
  return v;
}

/** A B+tree key (a tuple of values), shortened for display inside a node. */
export function formatKey(key: Value[], maxChars = 9): string {
  return key
    .map((v) => {
      const text = formatValue(v);
      return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
    })
    .join(" · ");
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: n < 10 ? 1 : 0 });
}

/** Which tree (or table's overflow pages) a page belongs to, for colouring and labelling pages. */
export interface PageOwner {
  tree: string;
  color: number;
  leaf: boolean;
  overflow?: boolean;
}

export function ownerLabel(owner: PageOwner): string {
  return `${owner.tree} ${owner.overflow ? "overflow page (TOAST)" : owner.leaf ? "leaf" : "node"}`;
}
