import { useEffect, useMemo, useRef, useState } from "react";
import type { Tree } from "../api";
import type { Flash } from "../feed";
import { NODE_HEIGHT, layoutTree } from "../layout";

interface Props {
  trees: Tree[];
  selected: string;
  onSelect: (name: string) => void;
  nodeFlash: Map<number, Flash>;
  flashMs: number;
}

const ZOOMS = [0.5, 0.75, 1, 1.25];
const MARGIN = 24;

export function TreeView({ trees, selected, onSelect, nodeFlash, flashMs }: Props) {
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const scroller = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState(800);
  const rootCentreRef = useRef(0);
  const tree = trees.find((t) => t.name === selected) ?? trees.find((t) => t.kind === "table") ?? trees[0];
  const hasTree = tree !== undefined;

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setAvailable(el.clientWidth));
    observer.observe(el);
    setAvailable(el.clientWidth);
    return () => observer.disconnect();
  }, [hasTree]);


  const tableKey = tree?.kind === "index" ? trees.find((t) => t.name === tree.table)?.key_columns.length ?? 0 : 0;
  const visibleColumns = tree ? Math.max(1, tree.key_columns.length - tableKey) : 1;
  const layout = useMemo(() => (tree ? layoutTree(tree, visibleColumns) : null), [tree, visibleColumns]);

  // When the tree is wider than the panel, keep its root in view: scroll to centre it whenever the
  // tree, its shape or the zoom changes.
  const shapeKey = tree ? `${tree.name}:${tree.nodes.length}:${zoom}` : "";
  useEffect(() => {
    const el = scroller.current;
    if (el && el.scrollWidth > el.clientWidth) el.scrollLeft = Math.max(0, rootCentreRef.current - el.clientWidth / 2);
  }, [shapeKey]);

  if (!tree || !layout) return <section className="panel tree-panel" aria-label="B+tree" />;

  const now = performance.now();
  const leaves = tree.nodes.filter((n) => n.leaf);
  const levels = Math.max(...layout.nodes.map((p) => p.depth)) + 1;
  const keys = leaves.reduce((sum, n) => sum + n.keys.length, 0);
  const margin = MARGIN;
  const fullWidth = layout.width + margin * 2;
  // Fit shrinks wide trees, but never below the size where keys stay readable; past that it scrolls.
  const scale = zoom === "fit" ? Math.min(1.25, Math.max(0.5, (available - 4) / fullWidth)) : zoom;
  const root = layout.nodes.find((p) => p.node.page === tree.root);
  const rootCentre = root ? (root.x + root.width / 2 + margin) * scale : 0;
  rootCentreRef.current = rootCentre;

  return (
    <section className="panel tree-panel" aria-label="B+tree">
      <header className="panel-head">
        <div className="tabs" role="tablist" aria-label="Trees">
          {trees
            .filter((t) => t.kind !== "catalog")
            .map((t) => (
              <button
                key={t.name}
                role="tab"
                aria-selected={t.name === tree.name}
                className={`tab ${t.name === tree.name ? "active" : ""}`}
                onClick={() => onSelect(t.name)}
              >
                {t.name}
                <span className="tab-kind">{t.kind === "index" ? "index" : "table"}</span>
              </button>
            ))}
        </div>
        <div className="zoom" role="group" aria-label="Zoom">
          <button className={`chip ${zoom === "fit" ? "active" : ""}`} onClick={() => setZoom("fit")}>
            Fit
          </button>
          {ZOOMS.map((z) => (
            <button key={z} className={`chip ${zoom === z ? "active" : ""}`} onClick={() => setZoom(z)}>
              {Math.round(z * 100)}%
            </button>
          ))}
        </div>
      </header>
      <p className="tree-caption">
        <strong>{tree.name}</strong> keyed by <code>{tree.key_columns.slice(0, visibleColumns).join(", ")}</code> ·{" "}
        {levels} {levels === 1 ? "level" : "levels"} · {tree.nodes.length} nodes · {leaves.length} leaves · {keys} keys
        {tree.truncated && " · showing the first nodes only"}
      </p>
      <div className="tree-scroll" ref={scroller}>
        <svg
          className="tree-svg"
          width={fullWidth * scale}
          height={(layout.height + margin * 2) * scale}
          viewBox={`${-margin} ${-margin} ${layout.width + margin * 2} ${layout.height + margin * 2}`}
          role="img"
          aria-label={`B+tree of ${tree.name} with ${tree.nodes.length} nodes`}
        >
          <defs>
            <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
              <path d="M0 0 8 4 0 8z" className="arrow-head" />
            </marker>
          </defs>
          {layout.edges.map((e) => {
            const lit = isLit(nodeFlash.get(e.to), now, flashMs);
            return (
              <path
                key={`${e.from}-${e.to}`}
                className={`edge ${lit ? "lit" : ""}`}
                d={`M${e.x1} ${e.y1} C${e.x1} ${(e.y1 + e.y2) / 2} ${e.x2} ${(e.y1 + e.y2) / 2} ${e.x2} ${e.y2}`}
              />
            );
          })}
          {layout.links.map((e) => (
            <path key={`link-${e.from}`} className="leaf-link" d={`M${e.x1} ${e.y1} L${e.x2} ${e.y2}`} markerEnd="url(#arrow)" />
          ))}
          {layout.nodes.map((p) => {
            const flash = nodeFlash.get(p.node.page);
            const active = flash && now - flash.at < flashMs ? flash : undefined;
            return (
              <g key={p.node.page} className="node" style={{ transform: `translate(${p.x}px, ${p.y}px)` }}>
                <title>
                  {`page ${p.node.page} · ${p.node.leaf ? "leaf" : "internal"} · ${p.node.keys.length} keys`}
                </title>
                <rect className={`node-box ${p.node.leaf ? "leaf" : "internal"}`} width={p.width} height={NODE_HEIGHT} rx={6} />
                {active && (
                  <rect
                    key={active.at}
                    className={`node-flash ${active.kind}`}
                    width={p.width}
                    height={NODE_HEIGHT}
                    rx={6}
                  />
                )}
                {p.labels.map((label, i) => {
                  const left = p.slots[i]!;
                  const right = p.slots[i + 1]!;
                  return (
                    <g key={i}>
                      {i > 0 && <line className="key-divider" x1={left} x2={left} y1={7} y2={NODE_HEIGHT - 7} />}
                      <text className="key-text" x={(left + right) / 2} y={NODE_HEIGHT / 2 + 4} textAnchor="middle">
                        {label}
                      </text>
                    </g>
                  );
                })}
                {p.labels.length === 0 && (
                  <text className="key-text empty" x={p.width / 2} y={NODE_HEIGHT / 2 + 4} textAnchor="middle">
                    empty
                  </text>
                )}
                <text className="page-tag" x={0} y={-5}>
                  p{p.node.page}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <footer className="legend">
        <span><i className="swatch internal" /> internal node</span>
        <span><i className="swatch leaf" /> leaf</span>
        <span><i className="swatch visit" /> visited</span>
        <span><i className="swatch split" /> split / merge</span>
        <span><i className="swatch link" /> leaf chain</span>
      </footer>
    </section>
  );
}

function isLit(flash: Flash | undefined, now: number, ms: number) {
  return flash !== undefined && flash.kind === "visit" && now - flash.at < ms;
}
