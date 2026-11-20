// The live event feed: a WebSocket to the engine, and a player that applies events at a chosen speed,
// so slow motion shows each page read, split and log write one by one.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type EngineEvent } from "./api";
import { describeEvent, type Tone } from "./events";

export type Speed = "instant" | "normal" | "slow";
const EVENTS_PER_SECOND: Record<Exclude<Speed, "instant">, number> = { normal: 120, slow: 4 };
// At normal speed a long backlog (a bulk insert, the crash demo) is played faster, so the view never
// trails the engine by more than about this many seconds. Slow motion stays slow.
const NORMAL_CATCH_UP_SECONDS = 3;
export const FLASH_MS: Record<Speed, number> = { instant: 500, normal: 700, slow: 1800 };
const MAX_QUEUE = 50_000;
const MAX_LOG = 300;
const MAX_WAL = 240;
const MAX_RECORDING = 5000; // events of one request kept for replay

export interface Flash {
  kind: string;
  at: number;
}

export interface WalFrame {
  id: number;
  page: number;
  commit: boolean;
  committed: boolean;
}

export interface LogLine {
  id: number;
  text: string;
  tone: Tone;
}

export type CrashPhase =
  | { phase: "idle" }
  | { phase: "writing"; batches: number }
  | { phase: "crashed"; batches: number }
  | { phase: "recovering"; frames: number }
  | { phase: "done" };

export interface Counters {
  hits: number;
  misses: number;
  writes: number;
  splits: number;
  logFrames: number;
  commits: number;
  checkpoints: number;
}

/**
 * The buffer pool as the engine holds it: pages in least-recently-used order (first = next to be
 * evicted), each in a fixed slot so the view can keep it in place while it stays cached.
 */
export interface Pool {
  size: number;
  /** page -> slot and whether it changed since the last commit, in LRU order. */
  pages: Map<number, { slot: number; dirty: boolean }>;
}

/** One request's events, and the feed state from just before them, for replaying step by step. */
export interface Recording {
  request: number;
  events: EngineEvent[];
  start: FeedState;
  truncated?: boolean;
}

export interface FeedState {
  pool: Pool;
  nodeFlash: Map<number, Flash>;
  pageFlash: Map<number, Flash>;
  wal: WalFrame[];
  checkpointAt: number | null;
  log: LogLine[];
  counters: Counters;
  crash: CrashPhase;
  /** The latest request's events (see replayFrame). */
  recording: Recording | null;
}

const emptyCounters = (): Counters => ({
  hits: 0,
  misses: 0,
  writes: 0,
  splits: 0,
  logFrames: 0,
  commits: 0,
  checkpoints: 0,
});

export type Connection = "connecting" | "live" | "offline";

/**
 * Apply one engine event to a feed state: flashes, the pool, the log, counters. Returns whether it
 * changed the shape of a tree (so the state needs fetching again). Used live and to rebuild replays.
 */
function reduce(s: FeedState, e: EngineEvent, now: number, nextId: () => number): boolean {
  const page = typeof e.page === "number" ? e.page : null;
  let structural = false;
  switch (e.kind) {
    case "page_read":
      if (page !== null) {
        s.pageFlash.set(page, { kind: e.source === "cache" ? "hit" : "miss", at: now });
        touch(s.pool, page, false);
      }
      if (e.source === "cache") s.counters.hits++;
      else s.counters.misses++;
      break;
    case "page_write":
    case "page_alloc":
      if (page !== null) {
        s.pageFlash.set(page, { kind: "write", at: now });
        touch(s.pool, page, true);
      }
      s.counters.writes++;
      break;
    case "cache_evict":
      if (page !== null) {
        s.pageFlash.set(page, { kind: "evict", at: now });
        s.pool.pages.delete(page);
      }
      break;
    case "page_free":
      if (page !== null) s.pageFlash.set(page, { kind: "evict", at: now });
      break;
    case "node_visit":
      if (page !== null) s.nodeFlash.set(page, { kind: "visit", at: now });
      break;
    case "node_split":
      if (page !== null) s.nodeFlash.set(page, { kind: "split", at: now });
      if (typeof e.new_page === "number") s.nodeFlash.set(e.new_page, { kind: "split", at: now });
      s.counters.splits++;
      structural = true;
      break;
    case "node_merge":
    case "node_redistribute":
    case "root_grow":
    case "root_shrink":
      if (page !== null) s.nodeFlash.set(page, { kind: "split", at: now });
      structural = true;
      break;
    case "wal_append":
      s.wal.push({ id: nextId(), page: page ?? 0, commit: Boolean(e.commit), committed: false });
      if (s.wal.length > MAX_WAL) s.wal.splice(0, s.wal.length - MAX_WAL);
      s.counters.logFrames++;
      break;
    case "wal_sync":
      for (const frame of s.wal) frame.committed = true;
      break;
    case "commit":
      s.counters.commits++;
      for (const entry of s.pool.pages.values()) entry.dirty = false; // logged, so no longer at risk
      break;
    case "rollback":
      s.wal = s.wal.filter((frame) => frame.committed);
      structural = true;
      break;
    case "checkpoint":
      s.checkpointAt = now;
      s.wal = [];
      s.counters.checkpoints++;
      break;
    case "demo_commit":
      s.crash = { phase: "writing", batches: Number(e.batch) || 0 };
      break;
    case "crash":
      s.crash = { phase: "crashed", batches: s.crash.phase === "writing" ? s.crash.batches : 0 };
      s.nodeFlash.clear();
      s.pageFlash.clear();
      s.pool.pages.clear(); // memory is lost; the next state sync shows what recovery loaded
      break;
    case "recovery":
      s.crash = { phase: "recovering", frames: Number(e.frames) || 0 };
      break;
    case "recovered":
      s.crash = { phase: "done" };
      structural = true;
      break;
  }
  const { text, tone } = describeEvent(e);
  s.log.push({ id: nextId(), text, tone });
  if (s.log.length > MAX_LOG) s.log.splice(0, s.log.length - MAX_LOG);
  return structural;
}

/** The parts of a feed state a replay starts from, copied so later events cannot change them. */
function copyState(s: Pick<FeedState, "pool" | "wal" | "counters" | "checkpointAt">): FeedState {
  return {
    pool: { size: s.pool.size, pages: new Map([...s.pool.pages].map(([page, entry]) => [page, { ...entry }])) },
    nodeFlash: new Map(),
    pageFlash: new Map(),
    wal: s.wal.map((frame) => ({ ...frame })),
    checkpointAt: s.checkpointAt,
    log: [],
    counters: { ...s.counters },
    crash: { phase: "idle" },
    recording: null,
  };
}

/**
 * The engine as it was just after event `index` of a recording: replays its events onto the state the
 * request started from. Each event's flash is dated `step` ms before the next, so the last few glow
 * like a trail and the current one is brightest.
 */
export function replayFrame(recording: Recording, index: number, step: number): FeedState {
  const s = copyState(recording.start);
  const now = performance.now();
  let id = 0;
  recording.events.slice(0, index + 1).forEach((e, i) => reduce(s, e, now - (index - i) * step, () => id++));
  return s;
}


export function useEngineFeed(sessionId: string | null, onSessionGone: () => void, onStructureChange: () => void) {
  const state = useRef<FeedState>({
    pool: { size: 0, pages: new Map() },
    nodeFlash: new Map(),
    pageFlash: new Map(),
    wal: [],
    checkpointAt: null,
    log: [],
    counters: emptyCounters(),
    crash: { phase: "idle" },
    recording: null,
  });
  const queue = useRef<EngineEvent[]>([]);
  const ids = useRef(0);
  const [speed, setSpeedState] = useState<Speed>("normal");
  const speedRef = useRef<Speed>("normal");
  const [connection, setConnection] = useState<Connection>("connecting");
  const [, setVersion] = useState(0);
  const dirty = useRef(false);
  const structureChanged = useRef(false);
  const callbacks = useRef({ onSessionGone, onStructureChange });
  callbacks.current = { onSessionGone, onStructureChange };

  const apply = useCallback((e: EngineEvent) => {
    const s = state.current;
    if (typeof e.request === "number") {
      // Keep the latest request's events, and the state it started from, so it can be replayed.
      if (s.recording?.request !== e.request) s.recording = { request: e.request, events: [], start: copyState(s) };
      if (s.recording.events.length < MAX_RECORDING) s.recording.events.push(e);
      else s.recording.truncated = true;
    }
    if (reduce(s, e, performance.now(), () => ids.current++)) structureChanged.current = true;
    dirty.current = true;
  }, []);

  // The player: drains the queue at the chosen rate, rendering at most once per animation frame.
  useEffect(() => {
    let frame = 0;
    let last = performance.now();
    let budget = 0;
    const tick = (now: number) => {
      const current = speedRef.current;
      if (current === "instant") {
        while (queue.current.length) apply(queue.current.shift()!);
        budget = 0;
      } else {
        const rate =
          current === "normal"
            ? Math.max(EVENTS_PER_SECOND.normal, queue.current.length / NORMAL_CATCH_UP_SECONDS)
            : EVENTS_PER_SECOND[current];
        budget = Math.min(budget + ((now - last) / 1000) * rate, Math.max(500, rate));
        while (budget >= 1 && queue.current.length) {
          apply(queue.current.shift()!);
          budget -= 1;
        }
        if (!queue.current.length) budget = Math.min(budget, 1);
      }
      last = now;
      if (structureChanged.current && !queue.current.length) {
        structureChanged.current = false;
        callbacks.current.onStructureChange();
      }
      if (dirty.current) {
        dirty.current = false;
        setVersion((v) => v + 1);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [apply]);

  // The socket: reconnects with backoff unless the session itself is gone.
  useEffect(() => {
    if (!sessionId) return;
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let stopped = false;
    const connect = () => {
      setConnection("connecting");
      socket = new WebSocket(api.eventsUrl(sessionId));
      socket.onopen = () => {
        retry = 0;
        setConnection("live");
      };
      socket.onmessage = (message) => {
        let data: { type?: string; events?: EngineEvent[] };
        try {
          data = JSON.parse(String(message.data));
        } catch {
          return;
        }
        if (data.type !== "events" || !Array.isArray(data.events)) return;
        for (const event of data.events) queue.current.push(event);
        if (queue.current.length > MAX_QUEUE) queue.current.splice(0, queue.current.length - MAX_QUEUE);
        dirty.current = true; // show the new queue length
      };
      socket.onclose = (event) => {
        if (stopped) return;
        setConnection("offline");
        if (event.code === 4404 || event.code === 4410) {
          callbacks.current.onSessionGone();
          return;
        }
        retry = Math.min(retry + 1, 6);
        timer = window.setTimeout(connect, 500 * 2 ** retry);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, [sessionId]);

  const setSpeed = useCallback((next: Speed) => {
    speedRef.current = next;
    setSpeedState(next);
  }, []);

  const skip = useCallback(() => {
    while (queue.current.length) apply(queue.current.shift()!);
  }, [apply]);

  const clearCrash = useCallback(() => {
    state.current.crash = { phase: "idle" };
    dirty.current = true;
  }, []);

  /**
   * Take the pool from a fresh engine state ([page, dirty], least recently used first). Ignored while
   * events are still queued: they describe how the pool got there and will bring it up to date.
   */
  const syncPool = useCallback((cached: { page: number; dirty: boolean }[], size: number) => {
    if (queue.current.length) return;
    const pool = state.current.pool;
    const previous = pool.pages;
    const used = new Set<number>();
    const next = new Map<number, { slot: number; dirty: boolean }>();
    for (const { page, dirty } of cached) {
      const slot = previous.get(page)?.slot;
      if (slot !== undefined && slot < size) used.add(slot);
      next.set(page, { slot: slot !== undefined && slot < size ? slot : -1, dirty });
    }
    let free = 0;
    for (const entry of next.values()) {
      if (entry.slot >= 0) continue;
      while (used.has(free)) free++;
      entry.slot = free;
      used.add(free);
    }
    pool.size = size;
    pool.pages = next;
    dirty.current = true;
  }, []);

  const startCrash = useCallback(() => {
    state.current.crash = { phase: "writing", batches: 0 };
    dirty.current = true;
  }, []);

  return {
    feed: state.current,
    pending: queue.current.length,
    speed,
    setSpeed,
    skip,
    connection,
    clearCrash,
    startCrash,
    syncPool,
  };
}

/**
 * A page was used: move it to the most recently used end, giving a newly cached page a free slot, or
 * when the pool is full the slot of the least recently used page (which the engine evicts right after).
 */
function touch(pool: Pool, page: number, write: boolean) {
  const entry = pool.pages.get(page);
  if (entry) {
    pool.pages.delete(page);
    pool.pages.set(page, { slot: entry.slot, dirty: entry.dirty || write });
    return;
  }
  if (!pool.size) return; // not seeded yet; the next state sync fills it in
  let slot: number;
  if (pool.pages.size >= pool.size) {
    const [victim, oldest] = pool.pages.entries().next().value!;
    pool.pages.delete(victim);
    slot = oldest.slot;
  } else {
    const used = new Set([...pool.pages.values()].map((p) => p.slot));
    slot = 0;
    while (used.has(slot)) slot++;
  }
  pool.pages.set(page, { slot, dirty: write });
}
