import type { Tree } from "../api";

/** The tree to show: the selected one if it still exists, else the first table. */
export function pickTree(trees: Tree[], selected: string | null): Tree | undefined {
  return trees.find((t) => t.name === selected) ?? trees.find((t) => t.kind === "table") ?? trees[0];
}

/** How many leading key columns to label: an index's own columns, without the table key it ends with. */
export function visibleKeyColumns(trees: Tree[], tree: Tree): number {
  const tableKey = tree.kind === "index" ? (trees.find((t) => t.name === tree.table)?.key_columns.length ?? 0) : 0;
  return Math.max(1, tree.key_columns.length - tableKey);
}

export function TreeTabs({ trees, current, onSelect }: { trees: Tree[]; current: Tree; onSelect: (name: string) => void }) {
  return (
    <div className="tabs" role="tablist" aria-label="Trees">
      {trees
        .filter((t) => t.kind !== "catalog")
        .map((t) => (
          <button
            key={t.name}
            role="tab"
            aria-selected={t.name === current.name}
            className={`tab ${t.name === current.name ? "active" : ""}`}
            onClick={() => onSelect(t.name)}
          >
            {t.name}
            <span className="tab-kind">{t.kind === "index" ? "index" : "table"}</span>
          </button>
        ))}
    </div>
  );
}

export function TreeCaption({ tree, visibleColumns, levels, extra }: {
  tree: Tree;
  visibleColumns: number;
  levels: number;
  extra?: string;
}) {
  const leaves = tree.nodes.filter((n) => n.leaf);
  const keys = leaves.reduce((sum, n) => sum + n.keys.length, 0);
  return (
    <p className="tree-caption">
      <strong>{tree.name}</strong> keyed by <code>{tree.key_columns.slice(0, visibleColumns).join(", ")}</code> ·{" "}
      {levels} {levels === 1 ? "level" : "levels"} · {tree.nodes.length} nodes · {leaves.length} leaves · {keys} keys
      {tree.truncated && " · showing the first nodes only"}
      {extra && ` · ${extra}`}
    </p>
  );
}
