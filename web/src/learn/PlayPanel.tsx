import { useState } from "react";
import { ApiError, type SessionName, type SessionTxn, type TableInfo } from "../api";
import { formatValue } from "../format";
import { EXAMPLES } from "../examples";
import { CHALLENGES } from "./challenges";
import { Editor } from "./Editor";
import type { Outcome } from "./outcome";

export const SECOND_SESSION_SQL = "SELECT id, name, city FROM customers WHERE id = 1;";

interface Props {
  two: boolean;
  onTwo: (two: boolean) => void;
  sqlB: string;
  onSqlB: (sql: string) => void;
  onShare: () => void;
  sql: string;
  onSql: (sql: string) => void;
  onRun: (sql: string, session: SessionName) => Promise<Outcome | ApiError | null>;
  sessions: Record<SessionName, SessionTxn> | null;
  maxSqlBytes: number | null;
  disabled: boolean;
  completed: Record<string, number>;
  tables: TableInfo[];
  /** The doctor's error span in the current text, if the last run failed on it. */
  error: { pos: number; end: number } | null;
}

/** One line about what a run did: its statements' statuses and the first value, or the error. */
function summary(result: Outcome | ApiError | null): { text: string; error: boolean } | null {
  if (!result) return null;
  if (result instanceof ApiError) return { text: `${result.body.type}: ${result.body.message}`, error: true };
  const parts = result.results.map((r) => {
    const first = r.rows[0]?.map(formatValue).join(", ");
    return r.columns.length && r.row_total ? `${r.status} → ${first}${r.row_total > 1 ? ", …" : ""}` : r.status;
  });
  return { text: parts.join(" · "), error: false };
}

export function PlayPanel(props: Props) {
  const { sql, onSql, onRun, sessions, maxSqlBytes, disabled, completed, tables, error } = props;
  const { two, onTwo: setTwo, sqlB, onSqlB: setSqlB } = props;
  const [showHints, setShowHints] = useState(false);
  const done = CHALLENGES.filter((c) => c.id in completed).length;

  return (
    <section className="side glass" aria-label="Playground">
      <header className="course-head">
        <p className="eyebrow">Playground</p>
        <h2>Write any SQL</h2>
        <p className="muted">
          Joins, subqueries, CTEs, window functions, transactions. Ctrl+Enter runs it; names complete as you type. Every run is
          checked by the advisor, and mistakes go to the doctor.
        </p>
      </header>
      <div className="play-tools">
      <div className="segmented small sessions-toggle" role="radiogroup" aria-label="Sessions">
        <button role="radio" aria-checked={!two} className={!two ? "active" : ""} onClick={() => setTwo(false)}>
          One session
        </button>
        <button role="radio" aria-checked={two} className={two ? "active" : ""} onClick={() => setTwo(true)}>
          Two sessions
        </button>
      </div>
      <button
        className="button ghost small"
        onClick={props.onShare}
        disabled={disabled}
        title="Copy a link that opens this query on a database set up the way yours is"
      >
        ⤴ Share
      </button>
      </div>
      {two && (
        <p className="muted small">
          Two connections to the same database, like two terminals running psql. BEGIN in one, change a row, and see what the
          other one sees.
        </p>
      )}
      <SessionEditor
        name="A"
        label={two ? "Session A" : null}
        sql={sql}
        onSql={onSql}
        onRun={onRun}
        txn={sessions?.A ?? null}
        maxSqlBytes={maxSqlBytes}
        disabled={disabled}
        tables={tables}
        error={error}
      />
      {two && (
        <SessionEditor
          name="B"
          label="Session B"
          sql={sqlB}
          onSql={setSqlB}
          onRun={onRun}
          txn={sessions?.B ?? null}
          maxSqlBytes={maxSqlBytes}
          disabled={disabled}
          tables={tables}
          error={null}
        />
      )}
      <h3 className="section-title">Try one</h3>
      <div className="examples" role="group" aria-label="Example queries">
        {EXAMPLES.map((example) => (
          <button key={example.label} className="chip" aria-label={example.label} title={example.hint} onClick={() => onSql(example.sql)}>
            {example.label}
          </button>
        ))}
      </div>
      <div className="section-row">
        <h3 className="section-title">
          Challenges <span className="muted">{done}/{CHALLENGES.length}</span>
        </h3>
        <button className="link small" onClick={() => setShowHints(!showHints)} aria-expanded={showHints}>
          {showHints ? "hide hints" : "show hints"}
        </button>
      </div>
      <ul className="challenges">
        {CHALLENGES.map((c) => {
          const complete = c.id in completed;
          return (
            <li key={c.id} className={`challenge ${complete ? "complete" : ""}`}>
              <span className="challenge-mark" aria-hidden="true">
                {complete ? "★" : "☆"}
              </span>
              <div>
                <strong>{c.title}</strong>
                <span className="visually-hidden">{complete ? " (completed)" : " (not yet)"}</span>
                <p>{c.goal}</p>
                {showHints && !complete && <p className="challenge-hint">{c.hint}</p>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SessionEditor({ name, label, sql, onSql, onRun, txn, maxSqlBytes, disabled, tables, error }: {
  name: SessionName;
  label: string | null;
  sql: string;
  onSql: (sql: string) => void;
  onRun: (sql: string, session: SessionName) => Promise<Outcome | ApiError | null>;
  txn: SessionTxn | null;
  maxSqlBytes: number | null;
  disabled: boolean;
  tables: TableInfo[];
  error: { pos: number; end: number } | null;
}) {
  const [running, setRunning] = useState(false);
  const [last, setLast] = useState<{ text: string; error: boolean } | null>(null);
  const tooLong = maxSqlBytes !== null && new TextEncoder().encode(sql).length > maxSqlBytes;

  const run = async () => {
    if (running || disabled || tooLong || !sql.trim()) return;
    setRunning(true);
    try {
      setLast(summary(await onRun(sql, name)));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className={label ? `session-box s-${name}` : ""}>
      {label && (
        <div className="session-head">
          <span className={`session-badge s-${name}`}>
            <span aria-hidden="true">{name}</span> {label}
          </span>
          <span className="muted small">
            {txn?.xid ? `transaction ${txn.xid} · ${txn.isolation?.toLowerCase()}` : "no transaction: each statement commits"}
          </span>
        </div>
      )}
      <label className="visually-hidden" htmlFor={`sql-editor-${name}`}>
        SQL for {label ?? "the playground"}
      </label>
      <Editor id={`sql-editor-${name}`} value={sql} onChange={onSql} onRun={() => void run()} tables={tables} error={error} />
      <div className="run-row">
        <button className="button primary" onClick={() => void run()} disabled={disabled || running || tooLong}>
          {running ? "Running…" : label ? `Run in ${name}` : "Run"}
        </button>
        {tooLong && <span className="muted small">SQL is limited to {maxSqlBytes?.toLocaleString()} bytes</span>}
      </div>
      {label && last && <p className={`session-last small ${last.error ? "error" : ""}`}>{last.text}</p>}
    </div>
  );
}
