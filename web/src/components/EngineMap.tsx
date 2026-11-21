// The engine map: the whole storage stack at once, the way a database textbook draws it. The B+tree a
// query walks sits on top; below it the buffer pool, the fixed set of page slots in memory, in the
// engine's own least-recently-used order; at the bottom the disk: the data file, page by page, and the
// write-ahead log. Every page a query touches lights up in each layer it passes through.
import { type PageOwner, ownerLabel } from "../format";
import { useEffect, useRef } from "react";
import type { EngineState, Tree } from "../api";
import type { FeedState, Flash } from "../feed";
import type { ReplayMarker } from "./Timeline";
import { TreeView } from "./TreeView";
import { Versions } from "./Versions";

export type MapFocus = "pool" | "disk" | "log" | null;

interface Props {
  trees: Tree[];
  selected: string | null;
  onSelect: (name: string) => void;
  feed: FeedState;
  flashMs: number;
  state: EngineState;
  owners: Map<number, PageOwner>;
  focus: MapFocus;
  marker: ReplayMarker | null;
}

const recent = (flash: Flash | undefined, now: number, ms: number) => (flash && now - flash.at < ms ? flash : undefined);

export function EngineMap({ trees, selected, onSelect, feed, flashMs, state, owners, focus, marker }: Props) {
  const poolRef = useRef<HTMLElement>(null);
  const diskRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const target = focus === "pool" ? poolRef.current : focus === "disk" || focus === "log" ? diskRef.current : null;
    target?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focus]);

  const now = performance.now();
  const recentFlashes = [...feed.pageFlash.values()].filter((f) => now - f.at < flashMs);
  const reading = recentFlashes.some((f) => f.kind === "miss");
  const logging = feed.wal.length > 0 && feed.wal.some((f) => !f.committed);
  const checkpointing = feed.checkpointAt !== null && now - feed.checkpointAt < flashMs * 2;

  return (
    <div className="engine-map">
      <section className="map-card map-tree" aria-label="B+tree">
        <TreeView trees={trees} selected={selected} onSelect={onSelect} nodeFlash={feed.nodeFlash} flashMs={flashMs} marker={marker} />
        <Descent trees={trees} selected={selected} feed={feed} />
      </section>

      <Versions state={state} />

      <Bus up={reading} upLabel="pages read from disk" down={false} downLabel="" />

      <section ref={poolRef} className={`map-card ${focus === "pool" ? "focused" : ""}`} aria-label="Buffer pool">
        <BufferPool feed={feed} flashMs={flashMs} owners={owners} now={now} current={marker?.page ?? null} />
      </section>

      <Bus up={false} upLabel="" down={logging || checkpointing} downLabel={checkpointing ? "checkpoint: log copied into the data file" : "changed pages logged"} />

      <section ref={diskRef} className={`map-card map-disk ${focus === "disk" || focus === "log" ? "focused" : ""}`} aria-label="Disk">
        <DataFile state={state} feed={feed} flashMs={flashMs} owners={owners} now={now} current={marker?.page ?? null} />
        <LogTape state={state} feed={feed} checkpointing={checkpointing} />
      </section>
    </div>
  );
}

/** The pages the last lookup descended through, root first. */
function Descent({ trees, selected, feed }: { trees: Tree[]; selected: string | null; feed: FeedState }) {
  const tree = trees.find((t) => t.name === selected) ?? trees.find((t) => t.kind === "table");
  const pages = new Map((tree?.nodes ?? []).map((n) => [n.page, n]));
  const visits = [...feed.nodeFlash.entries()].filter(([page, f]) => f.kind === "visit" && pages.has(page));
  const last = Math.max(0, ...visits.map(([, f]) => f.at));
  // One query's visits arrive close together; keep the burst that ended with the latest one.
  const path = visits.filter(([, f]) => last - f.at < 1500).sort((a, b) => a[1].at - b[1].at).map(([page]) => pages.get(page)!);
  if (!tree || !path.length) {
    return <p className="descent muted small">Run a query to see the path it takes from the root to the leaves.</p>;
  }
  const shown = path.slice(0, 8);
  return (
    <p className="descent small" aria-live="polite">
      <span className="muted">Last walk through {tree.name}:</span>
      {shown.map((n, i) => (
        <span key={n.page} className={`descent-step ${n.leaf ? "leaf" : "internal"}`}>
          {i > 0 && <i aria-hidden="true">→</i>}
          {n.page === tree.root ? "root " : n.leaf ? "leaf " : ""}p{n.page}
        </span>
      ))}
      {path.length > shown.length && <span className="muted">+{path.length - shown.length} more</span>}
      <span className="muted">· {path.length} page{path.length === 1 ? "" : "s"}</span>
    </p>
  );
}

/** A connector between two layers, animated while data moves across it. */
function Bus({ up, upLabel, down, downLabel }: { up: boolean; upLabel: string; down: boolean; downLabel: string }) {
  return (
    <div className={`bus ${up ? "up" : ""} ${down ? "down" : ""}`} aria-hidden="true">
      <span className="bus-line" />
      {(up || down) && <span className="bus-label">{up ? `↑ ${upLabel}` : `↓ ${downLabel}`}</span>}
    </div>
  );
}

function BufferPool({ feed, flashMs, owners, now, current }: {
  feed: FeedState;
  flashMs: number;
  owners: Map<number, PageOwner>;
  now: number;
  current: number | null;
}) {
  const { pool, counters } = feed;
  const slots: (number | null)[] = Array.from({ length: pool.size }, () => null);
  const rank = new Map<number, number>(); // 0 = least recently used
  let i = 0;
  for (const [page, entry] of pool.pages) {
    if (entry.slot < slots.length) slots[entry.slot] = page;
    rank.set(page, i++);
  }
  const full = pool.pages.size >= pool.size && pool.size > 0;
  const nextOut = full ? pool.pages.keys().next().value : undefined;
  const dirty = [...pool.pages.values()].filter((p) => p.dirty).length;
  const reads = counters.hits + counters.misses;

  return (
    <>
      <header className="map-head">
        <div>
          <h3>
            <span className="map-icon pool" aria-hidden="true">▦</span> Buffer pool
          </h3>
          <p className="muted small">
            {pool.size} page slots in memory. A page read from disk takes a free slot, or the least recently used page's.
          </p>
        </div>
        <dl className="map-stats">
          <div>
            <dt>in use</dt>
            <dd>
              {pool.pages.size}/{pool.size}
            </dd>
          </div>
          <div>
            <dt>hit rate</dt>
            <dd>{reads ? `${Math.round((counters.hits / reads) * 100)}%` : "–"}</dd>
          </div>
          <div>
            <dt>dirty</dt>
            <dd className={dirty ? "warn" : ""}>{dirty}</dd>
          </div>
        </dl>
      </header>
      <ol className="pool-grid" aria-label={`${pool.pages.size} of ${pool.size} slots in use`}>
        {slots.map((page, slot) => {
          if (page === null) return <li key={slot} className="slot empty" aria-label={`slot ${slot}: free`} />;
          const owner = owners.get(page);
          const entry = pool.pages.get(page)!;
          const flash = recent(feed.pageFlash.get(page), now, flashMs);
          const freshness = 0.62 + 0.38 * ((rank.get(page) ?? 0) / Math.max(1, pool.pages.size - 1));
          const label = `slot ${slot}: page ${page}${owner ? `, ${ownerLabel(owner)}` : ""}${entry.dirty ? ", changed since the last commit" : ""}${page === nextOut ? ", next to be evicted" : ""}`;
          return (
            <li
              key={slot}
              className={`slot owner-tint-${owner?.color ?? 7} ${entry.dirty ? "dirty" : ""} ${page === nextOut ? "next-out" : ""} ${page === current ? "current" : ""}`}
              style={{ opacity: freshness }}
              title={label}
              aria-label={label}
            >
              {flash && <span key={flash.at} className={`slot-flash ${flash.kind}`} />}
              <span className="slot-page">{page}</span>
            </li>
          );
        })}
      </ol>
      <p className="map-legend small">
        <span>
          <i className="dot hit" /> read from memory
        </span>
        <span>
          <i className="dot miss" /> loaded from disk
        </span>
        <span>
          <i className="dot write" /> changed
        </span>
        <span>
          <i className="dot dirty" /> not yet logged
        </span>
        <span>
          <i className="dot next" /> next to evict
        </span>
        <span className="muted">brighter = used more recently</span>
      </p>
    </>
  );
}

function DataFile({ state, feed, flashMs, owners, now, current }: {
  state: EngineState;
  feed: FeedState;
  flashMs: number;
  owners: Map<number, PageOwner>;
  now: number;
  current: number | null;
}) {
  const { page_count, free, page_size } = state.pager;
  const freeSet = new Set(free);
  const cached = feed.pool.pages;
  return (
    <div className="disk-part">
      <header className="map-head">
        <div>
          <h3>
            <span className="map-icon disk" aria-hidden="true">◫</span> Data file
          </h3>
          <p className="muted small">
            {page_count.toLocaleString()} pages of {page_size / 1024} KB on disk, coloured by the table or index that owns them.
          </p>
        </div>
      </header>
      <div className="file-grid" role="img" aria-label={`${page_count} pages, ${cached.size} of them also in memory`}>
        {Array.from({ length: page_count }, (_, page) => {
          const owner = owners.get(page);
          const flash = recent(feed.pageFlash.get(page), now, flashMs);
          const kind = page === 0 ? "header" : freeSet.has(page) ? "free" : owner ? `owner-${owner.color % 8}` : "unused";
          return (
            <span
              key={page}
              className={`file-page ${kind} ${cached.has(page) ? "cached" : ""} ${page === current ? "current" : ""}`}
              title={`page ${page} · ${page === 0 ? "file header" : freeSet.has(page) ? "free" : owner ? `${ownerLabel(owner)}` : "unused"}${cached.has(page) ? " · also in memory" : ""}`}
            >
              {flash?.kind === "miss" && <span key={flash.at} className="file-flash" />}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function LogTape({ state, feed, checkpointing }: { state: EngineState; feed: FeedState; checkpointing: boolean }) {
  const frames = feed.wal;
  const pending = frames.filter((f) => !f.committed).length;
  return (
    <div className={`disk-part wal ${checkpointing ? "checkpointing" : ""}`}>
      <header className="map-head">
        <div>
          <h3>
            <span className="map-icon log" aria-hidden="true">≋</span> Write-ahead log
          </h3>
          <p className="muted small">
            {frames.length
              ? `${frames.length} frames${pending ? `, ${pending} not yet synced` : ", all synced"}. A checkpoint copies them into the data file.`
              : `${state.wal.committed_frames} committed frames. Changes are logged here before the data file.`}
          </p>
        </div>
      </header>
      <div className="log-tape" aria-live="polite">
        {frames.length === 0 && <span className="muted small">Empty: every change so far is already in the data file.</span>}
        {frames.slice(-160).map((f) => (
          <span
            key={f.id}
            className={`log-frame ${f.commit ? "commit" : ""} ${f.committed ? "synced" : "pending"}`}
            title={f.commit ? "commit frame: the transaction is durable once this is synced" : `page ${f.page}${f.committed ? " · synced" : " · not yet synced"}`}
          >
            {f.commit ? "✓" : f.page}
          </span>
        ))}
      </div>
    </div>
  );
}
