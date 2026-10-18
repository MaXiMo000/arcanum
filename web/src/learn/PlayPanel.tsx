import { useState } from "react";
import type { TableInfo } from "../api";
import { EXAMPLES } from "../examples";
import { CHALLENGES } from "./challenges";
import { Editor } from "./Editor";

interface Props {
  sql: string;
  onSql: (sql: string) => void;
  onRun: (sql: string) => Promise<unknown>;
  maxSqlBytes: number | null;
  disabled: boolean;
  completed: Record<string, number>;
  tables: TableInfo[];
  /** The doctor's error span in the current text, if the last run failed on it. */
  error: { pos: number; end: number } | null;
}

export function PlayPanel({ sql, onSql, onRun, maxSqlBytes, disabled, completed, tables, error }: Props) {
  const [running, setRunning] = useState(false);
  const [showHints, setShowHints] = useState(false);
  const tooLong = maxSqlBytes !== null && new TextEncoder().encode(sql).length > maxSqlBytes;
  const done = CHALLENGES.filter((c) => c.id in completed).length;

  const run = async () => {
    if (running || disabled || tooLong || !sql.trim()) return;
    setRunning(true);
    try {
      await onRun(sql);
    } finally {
      setRunning(false);
    }
  };

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
      <label className="visually-hidden" htmlFor="sql-editor">
        SQL
      </label>
      <Editor id="sql-editor" value={sql} onChange={onSql} onRun={() => void run()} tables={tables} error={error} />
      <div className="run-row">
        <button className="button primary" onClick={() => void run()} disabled={disabled || running || tooLong}>
          {running ? "Running…" : "Run"}
        </button>
        {tooLong && <span className="muted small">SQL is limited to {maxSqlBytes?.toLocaleString()} bytes</span>}
      </div>
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
