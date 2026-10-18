// Where everything sits in the 3D vault, and the colours it glows in.
// The crystal tree reuses the 2D layout (so both views agree on node order) and bends it into a
// chandelier: the root at the centre, each deeper level on a wider ring, nodes around the ring in key
// order. A small tree stays almost flat; a wide one curves round so it fills the view. The page floor
// lies beneath and the log river spirals down beside it.
import { Color } from "three";
import type { Flash } from "../feed";
import type { Layout } from "../layout";

export const PX = 0.016; // world units per layout pixel
export const LEVEL_GAP = 2.6;
export const TREE_TOP = 6;
export const SLAB_HEIGHT = 0.5;
export const SLAB_DEPTH = 0.42;
export const FLOOR_Y = -3.2;
export const TILE_GAP = 0.34;
export const RIVER_RADIUS = 0.75;
const FLAT_WIDTH = 14; // trees narrower than this stay flat, like the 2D view
const ARC_PER_UNIT = 1 / 8; // radians of ring per world unit of flat width
const MAX_ARC = Math.PI * 1.7; // never close the ring completely, so the key order keeps a start and end

export const COLORS = {
  background: new Color("#04060f"),
  internal: new Color("#3c5ee8"),
  internalGlow: new Color("#2238a8"),
  leaf: new Color("#b98522"),
  leafGlow: new Color("#6b4708"),
  floor: new Color("#070b19"),
  platform: new Color("#0d1328"),
  hover: new Color("#8fd8ff"),
  visit: new Color("#fff1a6"),
  split: new Color("#ff5fd2"),
  edge: new Color("#33438a"),
  link: new Color("#7a6230"),
  edgeLit: new Color("#ffd76b"),
  spark: new Color("#fff4c4"),
  hit: new Color("#39e08c"),
  miss: new Color("#ffab33"),
  write: new Color("#ff5a3c"),
  evict: new Color("#262b36"),
  free: new Color("#151821"),
  header: new Color("#c7cfdf"),
  synced: new Color("#9a7bff"),
  pending: new Color("#4a3b8f"),
  commit: new Color("#ffd34d"),
  danger: new Color("#ff3b3b"),
};

// Matches the page owners in the 2D view: one colour per table or index, grey for the catalog.
export const OWNER_COLORS = ["#5b8cff", "#ffab4a", "#3fd68a", "#ff6fb5", "#a98bff", "#3fd0dc", "#f2d24b", "#8a8f99"].map(
  (hex) => new Color(hex),
);

export interface CrystalNode {
  page: number;
  leaf: boolean;
  labels: string[];
  x: number;
  y: number;
  z: number;
  angle: number; // rotation about the vertical axis, so the crystal follows its ring
  length: number;
}

export interface TreeEdge {
  from: number; // parent's position in nodes
  to: number; // child's position in nodes
  offset: number; // where along the parent the pointer leaves: the gap between the keys it separates
}

export interface Placement {
  nodes: CrystalNode[];
  index: Map<number, number>; // page -> position in nodes
  parent: Map<number, number>; // child page -> parent page
  edges: TreeEdge[];
  links: [number, number][]; // leaf chain: each leaf to the next, by position in nodes
  halfWidth: number; // farthest reach from the centre, sideways
  curved: boolean; // bent into a ring, so best seen from higher up
}

export function placeTree(layout: Layout): Placement {
  const flatWidth = layout.width * PX;
  const deepest = Math.max(1, ...layout.nodes.map((p) => p.depth));
  // Deep trees close their levels up so the leaves always stay above the page floor.
  const levelGap = Math.min(LEVEL_GAP, (TREE_TOP - FLOOR_Y - 1.6) / deepest);
  const arc = flatWidth <= FLAT_WIDTH ? 0 : Math.min(MAX_ARC, flatWidth * ARC_PER_UNIT);
  const radius = arc > 0 ? flatWidth / arc : 0; // the leaf ring keeps the flat layout's length
  const nodes = layout.nodes.map((p) => {
    const flatX = (p.x + p.width / 2 - layout.width / 2) * PX;
    const y = TREE_TOP - p.depth * levelGap;
    const length = Math.max(p.width * PX, 0.5);
    if (arc === 0) return { page: p.node.page, leaf: p.node.leaf, labels: p.labels, x: flatX, y, z: 0, angle: 0, length };
    const angle = (flatX / flatWidth) * arc;
    const r = radius * (p.depth / deepest);
    return {
      page: p.node.page,
      leaf: p.node.leaf,
      labels: p.labels,
      x: r * Math.sin(angle),
      y,
      z: r * Math.cos(angle) - radius / 2,
      angle,
      length,
    };
  });
  const index = new Map(nodes.map((n, i) => [n.page, i]));
  const parent = new Map<number, number>();
  const edges: TreeEdge[] = [];
  const links: [number, number][] = [];
  for (const p of layout.nodes) {
    p.node.children.forEach((child, i) => {
      const c = index.get(child);
      if (c === undefined) return;
      parent.set(child, p.node.page);
      const gap = p.slots[i] ?? p.width - 8;
      edges.push({ from: index.get(p.node.page)!, to: c, offset: (gap - p.width / 2) * PX });
    });
    const next = p.node.leaf && p.node.next ? index.get(p.node.next) : undefined;
    if (next !== undefined) links.push([index.get(p.node.page)!, next]);
  }
  const halfWidth = Math.max(3, ...nodes.map((n) => Math.abs(n.x) + n.length / 2));
  return { nodes, index, parent, edges, links, halfWidth, curved: arc > 0 };
}

/** Where page `page` sits on the floor grid. */
export function tilePosition(page: number, columns: number, rows: number): [number, number, number] {
  const col = page % columns;
  const row = Math.floor(page / columns);
  return [(col - (columns - 1) / 2) * TILE_GAP, FLOOR_Y, (row - (rows - 1) / 2) * TILE_GAP + 1];
}

export function floorShape(pageCount: number): { columns: number; rows: number } {
  const columns = Math.max(10, Math.min(48, Math.ceil(Math.sqrt(pageCount * 3))));
  return { columns, rows: Math.max(1, Math.ceil(pageCount / columns)) };
}

/** How strongly a flash still glows: 1 when it starts, 0 once `ms` have passed. */
export function glow(flash: Flash | undefined, now: number, ms: number): number {
  if (!flash) return 0;
  const age = now - flash.at;
  return age < 0 || age >= ms ? 0 : 1 - age / ms;
}

export function nextPowerOfTwo(n: number): number {
  let size = 16;
  while (size < n) size *= 2;
  return size;
}

export interface Motion {
  shatter: number; // 0 = whole; 1 = blown apart by a crash
  reduced: boolean; // the viewer asked for less motion
}
