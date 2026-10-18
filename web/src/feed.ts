// The live event feed: a WebSocket to the engine, and a player that applies events at a chosen speed,
// so slow motion shows each page read, split and log write one by one.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type EngineEvent } from "./api";
import { describeEvent, type Tone } from "./events";

export type Speed = "instant" | "normal" | "slow";
const EVENTS_PER_SECOND: Record<Exclude<Speed, "instant">, number> = { normal: 120, slow: 4 };
export const FLASH_MS: Record<Speed, number> = { instant: 500, normal: 700, slow: 1800 };
const MAX_QUEUE = 50_000;
const MAX_LOG = 300;
const MAX_WAL = 240;

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

interface FeedState {
  nodeFlash: Map<number, Flash>;
  pageFlash: Map<number, Flash>;
  wal: WalFrame[];
  checkpointAt: number | null;
  log: LogLine[];
  counters: Counters;
  crash: CrashPhase;
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

export function useEngineFeed(sessionId: string | null, onSessionGone: () => void, onStructureChange: () => void) {
  const state = useRef<FeedState>({
    nodeFlash: new Map(),
    pageFlash: new Map(),
    wal: [],
    checkpointAt: null,
    log: [],
    counters: emptyCounters(),
    crash: { phase: "idle" },
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
    const now = performance.now();
    const page = typeof e.page === "number" ? e.page : null;
    switch (e.kind) {
      case "page_read":
        if (page !== null) s.pageFlash.set(page, { kind: e.source === "cache" ? "hit" : "miss", at: now });
        if (e.source === "cache") s.counters.hits++;
        else s.counters.misses++;
        break;
      case "page_write":
      case "page_alloc":
        if (page !== null) s.pageFlash.set(page, { kind: "write", at: now });
        s.counters.writes++;
        break;
      case "cache_evict":
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
        structureChanged.current = true;
        break;
      case "node_merge":
      case "node_redistribute":
      case "root_grow":
      case "root_shrink":
        if (page !== null) s.nodeFlash.set(page, { kind: "split", at: now });
        structureChanged.current = true;
        break;
      case "wal_append":
        s.wal.push({ id: ids.current++, page: page ?? 0, commit: Boolean(e.commit), committed: false });
        if (s.wal.length > MAX_WAL) s.wal.splice(0, s.wal.length - MAX_WAL);
        s.counters.logFrames++;
        break;
      case "wal_sync":
        for (const frame of s.wal) frame.committed = true;
        break;
      case "commit":
        s.counters.commits++;
        break;
      case "rollback":
        s.wal = s.wal.filter((frame) => frame.committed);
        structureChanged.current = true;
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
        break;
      case "recovery":
        s.crash = { phase: "recovering", frames: Number(e.frames) || 0 };
        break;
      case "recovered":
        s.crash = { phase: "done" };
        structureChanged.current = true;
        break;
    }
    const { text, tone } = describeEvent(e);
    s.log.push({ id: ids.current++, text, tone });
    if (s.log.length > MAX_LOG) s.log.splice(0, s.log.length - MAX_LOG);
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
        budget = Math.min(budget + ((now - last) / 1000) * EVENTS_PER_SECOND[current], 500);
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
  };
}
