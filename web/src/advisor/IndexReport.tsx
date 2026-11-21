// The advisor across statements: indexes that would have helped several statements, and indexes that cost
// every write but nothing used, or that another index makes redundant.
import type { IndexReport as Report } from "../api";
import { formatCount } from "../format";

interface Props {
  report: Report;
  onRun: (sql: string) => void;
  busy: boolean;
}

export function IndexReport({ report, onRun, busy }: Props) {
  const { indexes, suggestions } = report;
  if (!indexes.length && !suggestions.length) return null;
  return (
    <section className="index-report" aria-label="Indexes across this session">
      <p className="eyebrow">Across this session</p>
      {suggestions.length > 0 && (
        <ul className="advice-list">
          {suggestions.map((s) => (
            <li key={s.sql} className="advice advice-warn">
              <span className="advice-badge warn">Workload</span>
              <strong className="advice-title">
                {s.label}: it would have helped {formatCount(s.statements)} statement{s.statements === 1 ? "" : "s"}
              </strong>
              <details>
                <summary className="muted small">Which statements</summary>
                <ul className="index-examples">
                  {s.examples.map((e) => (
                    <li key={e}>
                      <code>{e}</code>
                    </li>
                  ))}
                </ul>
              </details>
              <div className="advice-fix">
                <pre className="fix-sql">{s.sql}</pre>
                <button className="button primary small" disabled={busy} onClick={() => onRun(s.sql)}>
                  Create it
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {indexes.length > 0 && (
        <table className="index-table">
          <caption className="muted small">Every index, and the scans that used it since the database was opened</caption>
          <colgroup>
            <col />
            <col className="scans" />
            <col />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Index</th>
              <th scope="col">Scans</th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {indexes.map((index) => {
              const note = index.redundant
                ? `redundant: ${index.redundant} serves the same lookups`
                : index.unique
                  ? "unique: it enforces a constraint"
                  : index.scans === 0
                    ? "unused so far: every write still maintains it"
                    : "";
              const droppable = !index.unique && (index.redundant || index.scans === 0);
              return (
                <tr key={index.name} className={index.redundant ? "warn" : ""}>
                  <th scope="row">
                    <code>{index.name}</code>
                    <span className="muted small">
                      {" "}
                      {index.table} ({index.columns})
                    </span>
                  </th>
                  <td>{formatCount(index.scans)}</td>
                  <td>
                    <span className="muted small">{note}</span>
                    {droppable && (
                      <button className="chip" disabled={busy} onClick={() => onRun(`DROP INDEX ${index.name}`)}>
                        Drop
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
