// Typed client for the Arcanum API (see arcanum/server/app.py). Same origin only.

export type Value = null | boolean | number | string;

export interface PlanNode {
  id: number;
  operator: string;
  label: string;
  est_rows: number | null;
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
}

export interface QueryResult {
  columns: string[];
  rows: Value[][];
  truncated: boolean;
  row_total: number;
  rowcount: number;
  status: string;
  plan: PlanNode | null;
  advice: Advice[];
  /** The statement's own text within the script. */
  statement: string;
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
}

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
  query: (id: string, sql: string) =>
    call<{ results: QueryResult[]; stats: QueryStats }>("POST", `${sessionPath(id)}/query`, { sql }),
  state: (id: string) => call<EngineState>("GET", `${sessionPath(id)}/state`),
  crash: (id: string, duringWrite: boolean) =>
    call<CrashSummary>("POST", `${sessionPath(id)}/crash`, { during_write: duringWrite }),
  eventsUrl: (id: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${sessionPath(id)}/events`;
  },
};
