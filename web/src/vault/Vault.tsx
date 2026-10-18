// The 3D crystal vault: the selected B+tree as glowing crystals above a floor of pages, with the
// write-ahead log spiralling beside them. Loaded on demand; the 2D panels stay alongside it.
import { Canvas, useFrame, type ThreeEvent } from "@react-three/fiber";
import { useCallback, useMemo, useRef, useState, type RefObject } from "react";
import type { EngineState, Tree } from "../api";
import { TreeCaption, TreeTabs, pickTree, visibleKeyColumns } from "../components/TreeTabs";
import type { FeedState } from "../feed";
import { layoutTree } from "../layout";
import { CrystalTree } from "./CrystalTree";
import { LogRiver } from "./LogRiver";
import { PageFloor, type PageInfo } from "./PageFloor";
import { Bloom, Rig, type Bounds } from "./Stage";
import { COLORS, FLOOR_Y, RIVER_RADIUS, TREE_TOP, placeTree, type Motion } from "./world";

interface Props {
  trees: Tree[];
  selected: string | null;
  onSelect: (name: string) => void;
  feed: FeedState;
  flashMs: number;
  state: EngineState;
  owners: Map<number, { tree: string; color: number; leaf: boolean }>;
  onUnavailable: () => void;
}

interface Hover {
  text: string;
  x: number;
  y: number;
}

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

const prefersReducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

/** Eases the crash effect in and out: blown apart while the engine is down, reassembling during recovery. */
function CrashMotion({ feed, motion }: { feed: FeedState; motion: RefObject<Motion> }) {
  useFrame((_, delta) => {
    const m = motion.current;
    if (!m) return;
    const phase = feed.crash.phase;
    const target = phase === "crashed" ? 1 : phase === "recovering" ? 0.45 : 0;
    m.shatter = m.reduced ? target : m.shatter + (target - m.shatter) * (1 - Math.exp(-delta * 3.2));
  });
  return null;
}

export default function Vault({ trees, selected, onSelect, feed, flashMs, state, owners, onUnavailable }: Props) {
  const [supported] = useState(webglAvailable);
  const [hover, setHover] = useState<Hover | null>(null);
  const [frameCount, setFrameCount] = useState(0);
  const stage = useRef<HTMLDivElement>(null);
  const motion = useRef<Motion>({ shatter: 0, reduced: prefersReducedMotion() });

  const tree = pickTree(trees, selected);
  const visibleColumns = tree ? visibleKeyColumns(trees, tree) : 1;
  const layout = useMemo(() => (tree ? layoutTree(tree, visibleColumns) : null), [tree, visibleColumns]);
  const placement = useMemo(() => (layout ? placeTree(layout) : null), [layout]);

  const pages = useMemo<PageInfo[]>(() => {
    const cached = new Map(state.pager.cached.map((c) => [c.page, c.dirty]));
    const free = new Set(state.pager.free);
    return Array.from({ length: state.pager.page_count }, (_, page) => ({
      owner: owners.get(page),
      cached: cached.has(page),
      dirty: cached.get(page) === true,
      free: free.has(page),
    }));
  }, [state, owners]);

  // The scene runs from the tree's left edge to the log river beside its right edge.
  const halfWidth = placement?.halfWidth ?? 3;
  const riverX = halfWidth + 2.6;
  const left = -halfWidth - 1;
  const right = riverX + RIVER_RADIUS + 1;
  const bounds: Bounds = {
    centerX: (left + right) / 2,
    centerY: (TREE_TOP + FLOOR_Y) / 2,
    width: right - left,
    height: TREE_TOP - FLOOR_Y + 3,
  };

  const describe = useCallback(
    (kind: "node" | "page", page: number) => {
      if (kind === "node") {
        const node = placement?.nodes[placement.index.get(page) ?? -1];
        if (!node) return `page ${page}`;
        const keys = node.labels.length > 8 ? [...node.labels.slice(0, 8), "…"] : node.labels;
        return `page ${page} · ${node.leaf ? "leaf" : "internal"} · keys ${keys.join(", ") || "none"}`;
      }
      const info = pages[page];
      const owner = owners.get(page);
      const what = page === 0 ? "file header" : info?.free ? "free" : owner ? `${owner.tree} ${owner.leaf ? "leaf" : "node"}` : "unused";
      const where = info?.cached ? (info.dirty ? "cached, changed" : "cached") : "on disk";
      return `page ${page} · ${what} · ${where}`;
    },
    [placement, pages, owners],
  );

  const hoverHandler = useCallback(
    (kind: "node" | "page") => (page: number | null, event?: ThreeEvent<PointerEvent>) => {
      const box = stage.current?.getBoundingClientRect();
      if (page === null || !event || !box) {
        setHover(null);
        return;
      }
      setHover({ text: describe(kind, page), x: event.nativeEvent.clientX - box.left, y: event.nativeEvent.clientY - box.top });
    },
    [describe],
  );
  const onNodeHover = useMemo(() => hoverHandler("node"), [hoverHandler]);
  const onPageHover = useMemo(() => hoverHandler("page"), [hoverHandler]);

  if (!supported) {
    return (
      <section className="panel tree-panel vault-panel" aria-label="3D view">
        <p>This browser cannot draw 3D graphics (WebGL is unavailable).</p>
        <button className="button" onClick={onUnavailable}>
          Use the 2D view
        </button>
      </section>
    );
  }
  if (!tree || !placement) return <section className="panel tree-panel vault-panel" aria-label="3D view" />;

  const levels = Math.max(...(layout?.nodes.map((p) => p.depth) ?? [0])) + 1;

  return (
    <section className="panel tree-panel vault-panel" aria-label="3D view">
      <header className="panel-head">
        <TreeTabs trees={trees} current={tree} onSelect={onSelect} />
        <button className="chip" onClick={() => setFrameCount((n) => n + 1)}>
          Reset view
        </button>
      </header>
      <TreeCaption tree={tree} visibleColumns={visibleColumns} levels={levels} extra={`${state.pager.page_count} pages below`} />
      <div className="vault-stage" ref={stage}>
        <Canvas
          dpr={[1, 2]}
          camera={{ fov: 45, near: 0.1, far: 600, position: [0, 4, 30] }}
          gl={{ antialias: true, powerPreference: "high-performance" }}
          onCreated={({ gl }) => gl.domElement.addEventListener("webglcontextlost", onUnavailable)}
          aria-label={`3D view of ${tree.name}: ${tree.nodes.length} crystals for B+tree nodes, ${state.pager.page_count} floor tiles for pages, and the write-ahead log`}
          role="img"
        >
          <color attach="background" args={[COLORS.background]} />
          <fog attach="fog" args={[COLORS.background, 40, 160]} />
          <CrashMotion feed={feed} motion={motion} />
          <CrystalTree placement={placement} feed={feed} flashMs={flashMs} motion={motion} onHover={onNodeHover} />
          <PageFloor pages={pages} feed={feed} flashMs={flashMs} motion={motion} onHover={onPageHover} />
          <LogRiver feed={feed} x={riverX} />
          <Rig
            bounds={bounds}
            elevation={placement.curved ? 0.6 : 0.3}
            resetKey={`${tree.name}:${frameCount}`}
            fitKey={`${levels}:${Math.round(bounds.width / 4)}`}
          />
          <Bloom />
        </Canvas>
        {hover && (
          <div className="vault-tooltip" role="status" style={{ left: hover.x + 14, top: hover.y + 14 }}>
            {hover.text}
          </div>
        )}
        <p className="vault-hint">drag to orbit · scroll to zoom · right-drag to pan · hover for details</p>
      </div>
      <footer className="legend">
        <span><i className="swatch internal" /> internal node</span>
        <span><i className="swatch leaf" /> leaf</span>
        <span><i className="swatch visit" /> visited</span>
        <span><i className="swatch split" /> split / merge</span>
        <span><i className="swatch frame-synced" /> log frame</span>
        <span><i className="swatch frame-commit" /> commit</span>
        <span><i className="swatch flash-miss" /> read from disk</span>
        <span><i className="swatch flash-write" /> written</span>
      </footer>
    </section>
  );
}
