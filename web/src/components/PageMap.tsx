import { memo } from "react";
import type { EngineState } from "../api";
import type { Flash } from "../feed";

interface Props {
  state: EngineState;
  pageFlash: Map<number, Flash>;
  flashMs: number;
  owners: Map<number, { tree: string; color: number; leaf: boolean }>;
  treeNames: string[];
}

export function PageMap({ state, pageFlash, flashMs, owners, treeNames }: Props) {
  const { page_count, cached, free, max_pages, pool_size } = state.pager;
  const cache = new Map(cached.map((c) => [c.page, c.dirty]));
  const freeSet = new Set(free);
  const now = performance.now();
  const pages = Array.from({ length: page_count }, (_, i) => i);
  const dirtyCount = cached.filter((c) => c.dirty).length;

  return (
    <section className="panel" aria-label="Pages">
      <header className="panel-head">
        <h2>Pages</h2>
        <span className="muted small">
          {page_count} of {max_pages ?? "∞"} pages · {cached.length}/{pool_size} cached · {dirtyCount} dirty
        </span>
      </header>
      <div className="page-grid" role="list" aria-label={`${page_count} pages`}>
        {pages.map((page) => {
          const owner = owners.get(page);
          const flash = pageFlash.get(page);
          return (
            <PageCell
              key={page}
              page={page}
              header={page === 0}
              color={owner?.color}
              ownerName={owner ? `${owner.tree}${owner.leaf ? " leaf" : " node"}` : undefined}
              free={freeSet.has(page)}
              cached={cache.has(page)}
              dirty={cache.get(page) === true}
              flashKind={flash && now - flash.at < flashMs ? flash.kind : undefined}
              flashAt={flash?.at}
            />
          );
        })}
      </div>
      <footer className="legend">
        {treeNames.map((name, i) => (
          <span key={name}>
            <i className={`swatch owner-${i % 8}`} /> {name}
          </span>
        ))}
        <span><i className="swatch free" /> free</span>
        <span><i className="swatch cached" /> in buffer pool</span>
        <span><i className="swatch dirty" /> changed, not yet written</span>
      </footer>
      <footer className="legend">
        <span><i className="swatch flash-hit" /> read from cache</span>
        <span><i className="swatch flash-miss" /> read from disk</span>
        <span><i className="swatch flash-write" /> written</span>
      </footer>
    </section>
  );
}

interface CellProps {
  page: number;
  header: boolean;
  color?: number;
  ownerName?: string;
  free: boolean;
  cached: boolean;
  dirty: boolean;
  flashKind?: string;
  flashAt?: number;
}

const PageCell = memo(function PageCell(p: CellProps) {
  const describe = [
    `page ${p.page}`,
    p.header ? "file header" : p.free ? "free" : p.ownerName ?? "unused",
    p.cached ? (p.dirty ? "cached, changed" : "cached") : "on disk",
  ].join(" · ");
  const classes = [
    "page",
    p.header ? "header" : "",
    p.free ? "free" : "",
    p.color !== undefined ? `owner-${p.color % 8}` : "",
    p.cached ? "cached" : "",
    p.dirty ? "dirty" : "",
  ].join(" ");
  return (
    <div className={classes} role="listitem" title={describe} aria-label={describe}>
      {p.flashKind && <span key={p.flashAt} className={`page-flash ${p.flashKind}`} />}
    </div>
  );
});
