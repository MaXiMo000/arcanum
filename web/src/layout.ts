// Lays out a B+tree for drawing: leaves left to right in key order, each parent centred over its
// children, and every child pointer leaving its parent from the gap between the keys it separates.
import type { Tree, TreeNode } from "./api";
import { formatKey } from "./format";

export const NODE_HEIGHT = 34;
export const PAD = 8;
const CHAR_WIDTH = 7.4; // 12px monospace
const MIN_KEY_WIDTH = 26;
const LEAF_GAP = 18;
const LEVEL_GAP = 78;

export interface PlacedNode {
  node: TreeNode;
  x: number;
  y: number;
  width: number;
  depth: number;
  labels: string[];
  /** x of each key slot's left edge, relative to the node, plus the right edge at the end. */
  slots: number[];
}

export interface Edge {
  from: number;
  to: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Layout {
  nodes: PlacedNode[];
  edges: Edge[];
  links: Edge[];
  width: number;
  height: number;
}

export function layoutTree(tree: Tree, visibleColumns: number): Layout {
  const byPage = new Map(tree.nodes.map((n) => [n.page, n]));
  const depth = new Map<number, number>([[tree.root, 0]]);
  for (const n of tree.nodes) {
    for (const child of n.children) depth.set(child, (depth.get(n.page) ?? 0) + 1);
  }
  const placed = new Map<number, PlacedNode>();
  const shape = (n: TreeNode) => {
    const labels = n.keys.map((k) => formatKey(k.slice(0, visibleColumns)));
    const slots = [PAD];
    for (const label of labels) slots.push(slots[slots.length - 1]! + Math.max(MIN_KEY_WIDTH, label.length * CHAR_WIDTH + 14));
    if (!labels.length) slots.push(PAD + 44); // room for "empty"
    return { labels, slots, width: slots[slots.length - 1]! + PAD };
  };

  // Leaves in breadth-first order are already left to right.
  let x = 0;
  for (const n of tree.nodes) {
    if (!n.leaf) continue;
    const { labels, slots, width } = shape(n);
    const d = depth.get(n.page) ?? 0;
    placed.set(n.page, { node: n, x, y: d * LEVEL_GAP, width, depth: d, labels, slots });
    x += width + LEAF_GAP;
  }
  // Internal nodes bottom-up: centre each over its first and last placed child.
  const internal = tree.nodes.filter((n) => !n.leaf).sort((a, b) => (depth.get(b.page) ?? 0) - (depth.get(a.page) ?? 0));
  for (const n of internal) {
    const kids = n.children.map((c) => placed.get(c)).filter((p): p is PlacedNode => p !== undefined);
    const { labels, slots, width } = shape(n);
    const d = depth.get(n.page) ?? 0;
    let left: number;
    if (kids.length) {
      const first = kids[0]!;
      const last = kids[kids.length - 1]!;
      left = (first.x + last.x + last.width) / 2 - width / 2;
    } else {
      left = x; // children beyond the node limit were not loaded
      x += width + LEAF_GAP;
    }
    placed.set(n.page, { node: n, x: left, y: d * LEVEL_GAP, width, depth: d, labels, slots });
  }

  const nodes = [...placed.values()];
  const minX = Math.min(0, ...nodes.map((p) => p.x));
  for (const p of nodes) p.x -= minX;

  const edges: Edge[] = [];
  for (const p of nodes) {
    if (p.node.leaf) continue;
    p.node.children.forEach((child, i) => {
      const c = placed.get(child);
      if (!c) return;
      edges.push({
        from: p.node.page,
        to: child,
        x1: p.x + (p.slots[i] ?? p.width - PAD), // child i hangs from the gap before key i
        y1: p.y + NODE_HEIGHT,
        x2: c.x + c.width / 2,
        y2: c.y,
      });
    });
  }
  const links: Edge[] = [];
  for (const p of nodes) {
    const next = p.node.leaf && p.node.next ? placed.get(p.node.next) : undefined;
    if (next && byPage.has(next.node.page)) {
      links.push({
        from: p.node.page,
        to: next.node.page,
        x1: p.x + p.width,
        y1: p.y + NODE_HEIGHT / 2,
        x2: next.x,
        y2: next.y + NODE_HEIGHT / 2,
      });
    }
  }
  const width = Math.max(...nodes.map((p) => p.x + p.width), 0);
  const height = Math.max(...nodes.map((p) => p.y + NODE_HEIGHT), 0);
  return { nodes, edges, links, width, height };
}
