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
    case "lock_wait":
      return { text: `session ${e.session} waits for transaction ${num(e.holder)} of session ${e.holder_session}`, tone: "danger" };
    case "lock_granted":
      return { text: `session ${e.session} got its lock after ${num(e.waited_ms)} ms and goes on`, tone: "ok" };
    case "deadlock":
      return { text: `deadlock detected: session ${e.session}'s transaction ${num(e.victim)} rolled back`, tone: "danger" };
    case "savepoint":
      return { text: `session ${e.session}: savepoint ${e.name} starts subtransaction ${num(e.xid)}`, tone: "log" };
    case "release":
      return { text: `session ${e.session}: savepoint ${e.name} released, its changes join the transaction`, tone: "log" };
    case "rollback_to":
      return {
        text: `session ${e.session}: rolled back to savepoint ${e.name}, aborting transaction ids ${(e.aborted as number[]).join(", ")}`,
        tone: "danger",
      };
    case "constraint":
      return {
        text:
          e.constraint_kind === "referenced"
            ? `foreign key ${e.name}: looked in ${e.table} for rows referencing ${num(e.rows)} removed key${num(e.rows) === 1 ? "" : "s"}`
            : `checked ${e.constraint_kind} ${e.name} on ${num(e.rows)} row${num(e.rows) === 1 ? "" : "s"} of ${e.table}`,
        tone: "ok",
      };
    case "cascade":
      return {
        text: `foreign key ${e.constraint}: ${e.action === "CASCADE" ? "deleted" : "set to NULL in"} ${num(e.rows)} row${num(e.rows) === 1 ? "" : "s"} of ${e.table}`,
        tone: "write",
      };
    case "sequence":
      return {
        text: `sequence ${e.table}.${e.column} handed out ${num(e.first) === num(e.last) ? num(e.first) : `${num(e.first)}–${num(e.last)}`}`,
        tone: "write",
      };
    case "conflict":
      return {
        text: `ON CONFLICT on ${e.constraint}: ${num(e.rows)} row${num(e.rows) === 1 ? "" : "s"} ${e.action === "update" ? "updated" : e.action === "nothing" ? "skipped" : "left alone (WHERE was false)"}`,
        tone: "muted",
      };
    case "rw_conflict":
      return {
        text: `rw-dependency: transaction ${num(e.reader)} (${e.reader_session}) read ${e.what}, which ${num(e.writer)} (${e.writer_session}) changed`,
        tone: "write",
      };
    case "serialization_failure":
      return { text: `serialization failure: session ${e.session}'s transaction ${num(e.victim)} rolled back`, tone: "danger" };
    case "lock_timeout":
      return { text: `session ${e.session} gave up waiting for transaction ${num(e.holder)} (lock timeout)`, tone: "danger" };
    case "lock_cancel":
      return { text: `session ${e.session} cancelled its wait for transaction ${num(e.holder)}`, tone: "muted" };
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
