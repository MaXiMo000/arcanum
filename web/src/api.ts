// Typed client for the Arcanum API (see arcanum/server/app.py). Same origin only.

export type Value = null | boolean | number | string;

export interface PlanNode {
  id: number;
  operator: string;
  label: string;
  est_rows: number | null;
  /** The optimizer's estimated cost of this operator and everything below it, in row visits. */
  est_cost?: number | null;
  rows?: number;
  ms?: number;
  children: PlanNode[];
  subplans: { kind: string; correlated: boolean; plan: PlanNode }[];
}

/** A statement to run first (CREATE INDEX, ANALYZE), or the statement rewritten. */
export interface AdviceFix {
  kind: "run" | "rewrite";
  sql: string;
  label: string;
  original?: string;
}

/** One finding of the query advisor (arcanum/engine/advisor.py). */
export interface Advice {
  level: "warn" | "tip" | "good";
  code: string;
  title: string;
  detail: string;
  /** The plan node it is about. */
  node: number | null;
  fix: AdviceFix | null;
  /** For a suggested index: the optimizer's cost for the table now and with it (a hypothetical index). */
  estimate: { before: number; after: number; path: string } | null;
}

/** One decision the optimizer recorded while planning (arcanum/engine/optimizer.py). */
export interface PlannerDecision {
  phase: "rewrite" | "pushdown" | "access" | "join" | "order" | "index-only";
  title: string;
  detail: string;
  /** The plan node it produced, if it survived into the final plan. */
  node: number | null;
  candidates: { path: string; rows: number | null; cost: number | null; chosen: boolean; why: string }[];
}

export interface QueryResult {
  columns: string[];
  rows: Value[][];
  truncated: boolean;
  row_total: number;
  /** Values the server shortened because they were too long to send whole. */
  clipped?: number;
  rowcount: number;
  status: string;
  plan: PlanNode | null;
  advice: Advice[];
  /** The statement's own text within the script. */
  statement: string;
  planner: PlannerDecision[];
  planning_ms: number | null;
  execution_ms: number | null;
}

/** What the engine did for one request (counted exactly on the server). */
export interface QueryStats {
  elapsed_ms: number;
  cache_hits: number;
  disk_reads: number;
  nodes_visited: number;
  pages_written: number;
  pages_allocated: number;
  pages_freed: number;
  evictions: number;
  splits: number;
  merges: number;
  root_grew: number;
  root_shrank: number;
  log_frames: number;
  commits: number;
  checkpoints: number;
  rollbacks: number;
  sort_spills: number;
  /** Values moved to overflow pages (TOAST), and read back from them. */
  toast_writes?: number;
  toast_reads?: number;
  /** Constraint work: checks, foreign key cascades, sequence values and ON CONFLICT outcomes. */
  constraints?: ConstraintNote[];
}

export type ConstraintNote =
  | { kind: "constraint"; constraint_kind: "check" | "foreign key" | "referenced"; name: string; table: string; rows: number }
  | { kind: "cascade"; constraint: string; table: string; action: "CASCADE" | "SET NULL"; rows: number }
  | { kind: "sequence"; table: string; column: string; first: number; last: number }
  | { kind: "conflict"; table: string; action: "nothing" | "update" | "skipped"; constraint: string; rows: number };

/** The error doctor's diagnosis (arcanum/engine/doctor.py). */
export interface Doctor {
  hint: string;
  /** The whole script, corrected. */
  fix: string | null;
  fix_label: string | null;
  /** The corrected script from the failing statement on: what to run now. */
  rerun?: string;
  /** Characters at fault, as offsets into the script. */
  pos: number | null;
  end: number | null;
}

export interface ApiErrorBody {
  type: string;
  message: string;
  line?: number;
  column?: number;
  /** Statements before the failing one, which did run. */
  completed?: number;
  doctor?: Doctor;
  /** SERIALIZABLE: the read/write dependencies that made a transaction roll back. */
  dependencies?: Dependency[];
}

/** A read/write dependency: reader read something a concurrent writer then changed (or had changed). */
export interface Dependency {
  reader: number;
  writer: number;
  what: string;
  reader_session: string | null;
  writer_session: string | null;
}

export interface TreeNode {
  page: number;
  leaf: boolean;
  keys: Value[][];
  children: number[];
  next: number;
}

export interface Tree {
  kind: "catalog" | "table" | "index";
  name: string;
  table: string | null;
  root: number;
  key_columns: string[];
  nodes: TreeNode[];
  truncated: boolean;
  /** A table's overflow pages: values too large for its leaves (TOAST). */
  toast_pages?: number[];
}

export interface TableInfo {
  name: string;
  columns: { name: string; type: string }[];
  rows: number | null;
}

export interface Limits {
  query_timeout: number;
  max_rows: number;
  max_pages: number;
  max_sql_bytes: number;
  max_import_bytes: number;
}

/** The two database sessions each visitor gets, side by side. */
export type SessionName = "A" | "B";

/** A session's open transaction (if any) and the snapshot its next statement would read through. */
export interface SessionTxn {
  xid: number | null;
  isolation: string | null;
  snapshot: { xmin: number; xmax: number; active: number[]; fixed: boolean };
  /** Open savepoints, oldest first, each with the transaction id it writes with. */
  savepoints?: { name: string; xid: number }[];
}

export type TxnStatus = "committed" | "running" | "aborted";

/** One version of a row: created by xmin, deleted by xmax, and which sessions currently see it. */
export interface RowVersion {
  xmin: number;
  xmax: number | null;
  row: Value[];
  status: [TxnStatus, TxnStatus | null];
  seen_by: SessionName[];
}

/** A statement parked behind another transaction's lock: fetch its ticket until it finishes. */
export interface WaitInfo {
  ticket: string;
  session: SessionName;
  holder: number;
  holder_session: string | null;
  /** What it needs, e.g. "row (id = 1)". */
  what: string;
  waited_ms: number;
}

export interface QueryReply {
  results: QueryResult[];
  stats: QueryStats;
  /** Set while a statement of the script waits for a lock; results holds the statements before it. */
  waiting?: WaitInfo;
}

export interface EngineState {
  tables: TableInfo[];
  trees: Tree[];
  pager: {
    page_size: number;
    page_count: number;
    max_pages: number | null;
    pool_size: number;
    cached: { page: number; dirty: boolean }[];
    free: number[];
    stats: Record<string, number>;
  };
  wal: { committed_frames: number; uncommitted: boolean };
  in_transaction: boolean;
  limits: Limits;
  sessions: Record<SessionName, SessionTxn>;
  running: { xid: number; session: string; isolation: string }[];
  next_xid: number;
  commit_log: number[];
  /** Rows that currently hold more than one version, or one not yet frozen. */
  versions: { table: string; key: Value[]; versions: RowVersion[] }[];
  /** Who waits for whom: each parked statement and the transaction it waits for. */
  waits: (WaitInfo & { xid: number | null })[];
  /** SERIALIZABLE transactions the engine still tracks: running, or committed but overlapping one that is. */
  serializable: { xid: number; session: string; committed: boolean; reads: string[] }[];
  dependencies: Dependency[];
  indexes: IndexReport;
}

/** The advisor across statements (Database.index_report). */
export interface IndexReport {
  indexes: { name: string; table: string; columns: string; unique: boolean; scans: number; redundant: string | null }[];
  suggestions: { sql: string; label: string; statements: number; examples: string[] }[];
}

export interface EngineEvent {
  kind: string;
  generation: number;
  seq?: number;
  [field: string]: unknown;
}

export interface CrashSummary {
  recovered_frames: number;
  during_write: boolean;
  acknowledged_rows?: number;
  rows_after_recovery?: number;
  in_flight_rows?: number;
  in_flight_committed?: boolean;
  consistent?: boolean;
}

export interface SessionInfo {
  id: string;
  recovered_frames: number;
  tables: TableInfo[];
  limits: Limits;
}

/** One engine's measurements in the B+tree versus LSM tree comparison. */
export interface EngineResult {
  write_amp: number;
  log_bytes: number;
  data_bytes: number;
  syncs: number;
  write_ms: number;
  space_amp: number;
  disk_bytes: number;
  lookup_reads: number;
  scan_reads: number;
  ms: number;
}

export interface LsmFile {
  number: number;
  bytes: number;
  entries: number;
  first: number;
  last: number;
}

export interface LsmShape {
  memtable: { entries: number; bytes: number; limit: number };
  levels: LsmFile[][];
  capacity: (number | null)[];
  written: number;
}

export interface LsmMoment extends LsmShape {
  op: number;
  kind: "flush" | "compaction" | "move";
  detail: { table?: number; bytes?: number; from?: number; to?: number; inputs?: number[]; overlapping?: number[];
    outputs?: number[]; bytes_in?: number; bytes_out?: number };
}

export interface LabParams {
  keys: number;
  order: "sequential" | "random";
  updates: number;
  value_bytes: number;
  memory_pages: number;
}

export interface LabResult {
  workload: LabParams & { operations: number; logical_bytes: number; live_bytes: number; lookups: number; scans: number; batch: number };
  btree: EngineResult & { height: number; pages: number; checkpoints: number };
  lsm: EngineResult & { flushes: number; compactions: number; moves: number; bloom_skips: number; filter_memory: number; levels: number };
  /** [operations done, B+tree bytes written, LSM bytes written] */
  series: [number, number, number][];
  shapes: LsmMoment[];
  final: LsmShape;
  key_space: number;
}

export type PoolPolicy = "lru" | "clock" | "2q" | "optimal";

export interface PoolLabParams {
  workload: "hot" | "hot-scan" | "loop" | "index";
  pool_pages: number;
  rows: number;
  lookups: number;
}

export interface PoolLabResult {
  workload: PoolLabParams & { name: PoolLabParams["workload"]; about: string; accesses: number; distinct_pages: number; table_pages: number };
  /** series: [accesses so far, hit rate over the recent window] */
  policies: Record<PoolPolicy, { hits: number; misses: number; hit_rate: number; series: [number, number][] }>;
  sweep: { sizes: number[]; hit_rates: Record<PoolPolicy, number[]> };
}

export type NodeName = "n1" | "n2" | "n3";

export interface ClusterNode {
  name: NodeName;
  up: boolean;
  role: "follower" | "candidate" | "leader";
  term: number;
  voted_for: NodeName | null;
  leader: NodeName | null;
  commit: number;
  applied: number;
  /** The term of each log entry, position 1 first. */
  log: number[];
  /** Ticks until this node stands for election; null for a leader or a node that is down. */
  election_in: number | null;
  /** Leader only: the highest log position known to be stored on each node. */
  match: Record<NodeName, number> | null;
}

export interface ClusterMessage {
  id: number;
  kind: "vote" | "vote_reply" | "append" | "append_reply";
  from: NodeName;
  to: NodeName;
  term: number;
  sent: number;
  deliver: number;
  /** Entries carried by an AppendEntries; 0 for a heartbeat. */
  size: number;
  granted: boolean | null;
  success: boolean | null;
}

export interface ClusterState {
  now: number;
  nodes: ClusterNode[];
  messages: ClusterMessage[];
  groups: NodeName[][];
}

export interface ClusterEntry {
  /** null for the no-op a new leader appends. */
  sql: string | null;
  result?: { ok: boolean; rowcount?: number; error?: string };
}

export interface ClusterReply {
  state: ClusterState;
  /** Keyed "position:term". */
  entries: Record<string, ClusterEntry>;
  history: { tick: number; text: string }[];
  frames?: ClusterState[];
  position?: number;
  read?: { columns: string[]; rows: Value[][]; applied: number; commit: number; role: ClusterNode["role"]; term: number };
}

export type ClusterRequest =
  | { action: "state" | "heal" | "reset" }
  | { action: "tick"; ticks: number }
  | { action: "write"; sql: string; node?: NodeName }
  | { action: "read"; sql: string; node: NodeName }
  | { action: "partition"; groups: NodeName[][] }
  | { action: "crash" | "restart"; node: NodeName };

/** What a CSV import would do (POST /sessions/{id}/import with dry_run). */
export interface ImportPreview {
  table: string;
  create: boolean;
  rows_total: number;
  columns: { name: string; type: string }[];
  sample: string[][];
}

export interface ImportRequest {
  table: string;
  csv: string;
  header: boolean;
}

export interface ScalingQuery {
  title: string;
  about: string;
  sql: string;
  /** One per size: line items, orders, pages asked for, rows returned, time. */
  points: { rows: number; orders: number; pages: number; rows_out: number; ms: number }[];
  slope: number;
  growth: "logarithmic" | "sublinear" | "linear";
}

export interface ScalingResult {
  sizes: number[];
  queries: Record<string, ScalingQuery>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: ApiErrorBody,
  ) {
    super(body.message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, { type: "NetworkError", message: "the server cannot be reached" });
  }
  if (response.status === 204) return undefined as T;
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    // An empty or non-JSON body; handled below.
  }
  if (!response.ok) {
    const error = (data as { error?: ApiErrorBody } | null)?.error;
    throw new ApiError(response.status, error ?? { type: "HttpError", message: `request failed (${response.status})` });
  }
  return data as T;
}

const sessionPath = (id: string) => `/sessions/${encodeURIComponent(id)}`;

export const api = {
  createSession: () => call<SessionInfo>("POST", "/sessions"),
  deleteSession: (id: string) => call<void>("DELETE", sessionPath(id)),
  query: (id: string, sql: string, session: SessionName = "A") =>
    call<QueryReply>("POST", `${sessionPath(id)}/query`, { sql, session }),
  ticket: (id: string, ticket: string) => call<QueryReply>("GET", `${sessionPath(id)}/tickets/${encodeURIComponent(ticket)}`),
  cancel: (id: string, ticket: string) =>
    call<QueryReply>("POST", `${sessionPath(id)}/tickets/${encodeURIComponent(ticket)}/cancel`),
  state: (id: string) => call<EngineState>("GET", `${sessionPath(id)}/state`),
  lab: (id: string, params: LabParams) => call<LabResult>("POST", `${sessionPath(id)}/labs/engines`, params),
  scalingLab: (id: string) => call<ScalingResult>("POST", `${sessionPath(id)}/labs/scaling`, {}),
  poolLab: (id: string, params: PoolLabParams) => call<PoolLabResult>("POST", `${sessionPath(id)}/labs/buffer-pool`, params),
  cluster: (id: string, request: ClusterRequest) => call<ClusterReply>("POST", `${sessionPath(id)}/cluster`, request),
  previewImport: (id: string, request: ImportRequest) =>
    call<ImportPreview>("POST", `${sessionPath(id)}/import`, { ...request, dry_run: true }),
  importCsv: (id: string, request: ImportRequest, session: SessionName = "A") =>
    call<QueryReply>("POST", `${sessionPath(id)}/import`, { ...request, session }),
  crash: (id: string, duringWrite: boolean) =>
    call<CrashSummary>("POST", `${sessionPath(id)}/crash`, { during_write: duringWrite }),
  eventsUrl: (id: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${sessionPath(id)}/events`;
  },
};
