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

export interface QueryResult {
  columns: string[];
  rows: Value[][];
  truncated: boolean;
  row_total: number;
  rowcount: number;
  status: string;
  plan: PlanNode | null;
}

export interface ApiErrorBody {
  type: string;
  message: string;
  line?: number;
  column?: number;
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
  limits: { query_timeout: number; max_rows: number; max_pages: number; max_sql_bytes: number };
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
  query: (id: string, sql: string) => call<{ results: QueryResult[] }>("POST", `${sessionPath(id)}/query`, { sql }),
  state: (id: string) => call<EngineState>("GET", `${sessionPath(id)}/state`),
  crash: (id: string, duringWrite: boolean) =>
    call<CrashSummary>("POST", `${sessionPath(id)}/crash`, { during_write: duringWrite }),
  eventsUrl: (id: string) => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}${sessionPath(id)}/events`;
  },
};
