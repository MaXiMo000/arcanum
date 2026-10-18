// Human-readable descriptions of engine events, for the event log.
import type { EngineEvent } from "./api";

export type Tone = "read" | "hit" | "write" | "log" | "tree" | "danger" | "ok" | "muted";

const num = (v: unknown) => (typeof v === "number" ? v : Number(v));

function keyText(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? `'${x}'` : String(x))).join(", ");
  return String(v);
}

export function describeEvent(e: EngineEvent): { text: string; tone: Tone } {
  switch (e.kind) {
    case "page_read":
      return e.source === "cache"
        ? { text: `read page ${num(e.page)} from the buffer pool`, tone: "hit" }
        : { text: `read page ${num(e.page)} from disk`, tone: "read" };
    case "page_write":
      return { text: `change page ${num(e.page)} in memory`, tone: "write" };
    case "cache_evict":
      return {
        text: e.dirty
          ? `evict page ${num(e.page)}: its unsaved change goes to the log`
          : `evict page ${num(e.page)} from the buffer pool`,
        tone: "muted",
      };
    case "page_alloc":
      return { text: `allocate page ${num(e.page)}`, tone: "write" };
    case "page_free":
      return { text: `free page ${num(e.page)}`, tone: "muted" };
    case "node_visit":
      return { text: `visit ${e.leaf ? "leaf" : "internal"} node on page ${num(e.page)}`, tone: "tree" };
    case "node_split":
      return {
        text: `split node ${num(e.page)}: upper half moves to new page ${num(e.new_page)}, separator ${keyText(e.separator)} goes up`,
        tone: "tree",
      };
    case "node_merge":
      return { text: `merge node ${num(e.removed_page)} into node ${num(e.page)}`, tone: "tree" };
    case "node_redistribute":
      return { text: `rebalance keys between nodes ${num(e.page)} and ${num(e.sibling_page)}`, tone: "tree" };
    case "root_grow":
      return { text: "the root split: the tree is one level taller", tone: "tree" };
    case "root_shrink":
      return { text: "the root shrank: the tree is one level shorter", tone: "tree" };
    case "wal_append":
      return e.commit
        ? { text: "append the commit frame to the log", tone: "log" }
        : { text: `append page ${num(e.page)} to the log`, tone: "log" };
    case "wal_sync":
      return { text: "fsync the log: the transaction is durable", tone: "ok" };
    case "commit":
      return { text: `commit (${num(e.pages)} changed pages)`, tone: "ok" };
    case "rollback":
      return { text: "roll back: changed pages restored", tone: "muted" };
    case "checkpoint":
      return { text: `checkpoint: ${num(e.pages)} pages copied from the log to the data file`, tone: "log" };
    case "sort_spill":
      return { text: `sort spilled ${num(e.runs)} sorted runs to disk (${num(e.rows)} rows)`, tone: "write" };
    case "demo_commit":
      return { text: `crash demo: batch ${num(e.batch)} committed`, tone: "ok" };
    case "crash":
      return { text: "power cut: the engine process was killed", tone: "danger" };
    case "recovery":
      return { text: `recovery: replaying ${num(e.frames)} committed log frames`, tone: "ok" };
    case "recovered":
      return { text: "recovery finished", tone: "ok" };
    case "worker_restart":
      return {
        text: e.reason === "timeout" ? "statement took too long: engine restarted" : "engine restarted",
        tone: "danger",
      };
    case "events_truncated":
      return { text: `more than ${num(e.limit)} events: the rest were not streamed`, tone: "muted" };
    default:
      return { text: e.kind.replace(/_/g, " "), tone: "muted" };
  }
}
