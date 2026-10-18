import { useRef, useState, type KeyboardEvent } from "react";
import { ApiError, type QueryResult } from "../api";
import { EXAMPLES } from "../examples";
import { formatValue } from "../format";
import { PlanView } from "./PlanView";

interface Props {
  disabled: boolean;
  maxSqlBytes: number | null;
  onRun: (sql: string) => Promise<QueryResult[]>;
}

const SHOWN_ROWS = 100;

export function QueryPanel({ disabled, maxSqlBytes, onRun }: Props) {
  const [sql, setSql] = useState(EXAMPLES[0]!.sql);
  const [results, setResults] = useState<QueryResult[] | null>(null);
  const [error, setError] = useState<{ error: ApiError; sql: string } | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const editor = useRef<HTMLTextAreaElement>(null);
  const tooLong = maxSqlBytes !== null && new TextEncoder().encode(sql).length > maxSqlBytes;

  const run = async () => {
    if (running || disabled || tooLong || !sql.trim()) return;
    setRunning(true);
    setError(null);
    const started = performance.now();
    try {
      setResults(await onRun(sql));
    } catch (e) {
      setResults(null);
      setError({
        error: e instanceof ApiError ? e : new ApiError(0, { type: "Error", message: String(e) }),
        sql,
      });
    } finally {
      setElapsed(performance.now() - started);
      setRunning(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void run();
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      const el = e.currentTarget;
      const { selectionStart: start, selectionEnd: end } = el;
      const next = `${sql.slice(0, start)}  ${sql.slice(end)}`;
      setSql(next);
      requestAnimationFrame(() => el.setSelectionRange(start + 2, start + 2));
    }
  };

  return (
    <section className="panel query-panel" aria-label="Query">
      <header className="panel-head">
        <h2>Query</h2>
        <span className="muted small">Ctrl+Enter to run</span>
      </header>
      <div className="examples" role="group" aria-label="Example queries">
        {EXAMPLES.map((example) => (
          <button
            key={example.label}
            className="chip"
            aria-label={example.label}
            title={example.hint}
            onClick={() => {
              setSql(example.sql);
              editor.current?.focus();
            }}
          >
            {example.label}
          </button>
        ))}
      </div>
      <label className="visually-hidden" htmlFor="sql-editor">
        SQL
      </label>
      <textarea
        id="sql-editor"
        ref={editor}
        className="editor"
        value={sql}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        onChange={(e) => setSql(e.target.value)}
        onKeyDown={onKeyDown}
        rows={7}
      />
      <div className="run-row">
        <button className="button primary" onClick={() => void run()} disabled={disabled || running || tooLong}>
          {running ? "Running…" : "Run"}
        </button>
        <span className="muted small" aria-live="polite">
          {tooLong
            ? `SQL is limited to ${maxSqlBytes?.toLocaleString()} bytes`
            : elapsed !== null && !running
              ? `${elapsed.toFixed(0)} ms round trip`
              : ""}
        </span>
      </div>
      {error && <ErrorBox error={error.error} sql={error.sql} />}
      {results?.map((result, i) => <ResultView key={i} result={result} />)}
    </section>
  );
}

function ErrorBox({ error, sql }: { error: ApiError; sql: string }) {
  const { line, column, type, message } = error.body;
  const source = line !== undefined ? sql.split("\n")[line - 1] : undefined;
  return (
    <div className="error-box" role="alert">
      <strong>{type}</strong> {line !== undefined && <span className="muted">line {line}, column {column}</span>}
      <div>{message}</div>
      {source !== undefined && column !== undefined && (
        <pre className="caret">
          {source}
          {"\n"}
          {" ".repeat(Math.max(0, column - 1))}^
        </pre>
      )}
    </div>
  );
}

function ResultView({ result }: { result: QueryResult }) {
  const [showPlan, setShowPlan] = useState(true);
  const rows = result.rows.slice(0, SHOWN_ROWS);
  return (
    <div className="result">
      <div className="result-head">
        <span className="status">{result.status}</span>
        {result.columns.length > 0 && (
          <span className="muted small">
            {result.truncated || result.rows.length > SHOWN_ROWS
              ? `showing ${rows.length} of ${result.row_total.toLocaleString()} rows`
              : `${result.row_total.toLocaleString()} ${result.row_total === 1 ? "row" : "rows"}`}
          </span>
        )}
        {result.plan && (
          <button className="chip" onClick={() => setShowPlan(!showPlan)} aria-expanded={showPlan}>
            {showPlan ? "hide plan" : "show plan"}
          </button>
        )}
      </div>
      {result.columns.length > 0 && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {result.columns.map((c, i) => (
                  <th key={i} scope="col">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {row.map((v, c) => (
                    <td key={c} className={v === null ? "null" : typeof v === "number" ? "num" : ""}>
                      {formatValue(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {result.plan && showPlan && <PlanView plan={result.plan} />}
    </div>
  );
}
