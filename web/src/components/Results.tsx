import type { ApiError, QueryResult } from "../api";
import { SqlDiff } from "../advisor/Advisor";
import { formatValue } from "../format";

const SHOWN_ROWS = 100;

export interface RunError {
  error: ApiError;
  sql: string;
}

interface ErrorProps extends RunError {
  /** Apply the doctor's fix: the corrected script for the editor, and what to run now. */
  onFix?: (fixed: string, rerun: string) => void;
}

export function ErrorBox({ error, sql, onFix }: ErrorProps) {
  const { line, column, type, message, completed, doctor } = error.body;
  const source = line !== undefined ? sql.split("\n")[line - 1] : undefined;
  const width = doctor?.pos != null && doctor.end != null ? Math.max(1, doctor.end - doctor.pos) : 1;
  return (
    <div className="error-box" role="alert">
      <p className="error-head">
        <strong>{type}</strong>
        {line !== undefined && (
          <span className="muted">
            line {line}, column {column}
          </span>
        )}
      </p>
      <p className="error-message">{message}</p>
      {source !== undefined && column !== undefined && (
        <pre className="caret">
          {source}
          {"\n"}
          {" ".repeat(Math.max(0, column - 1))}
          {"^".repeat(Math.min(width, Math.max(1, source.length - column + 1)))}
        </pre>
      )}
      {completed ? (
        <p className="muted small">
          The {completed === 1 ? "statement" : `${completed} statements`} before this one ran and {completed === 1 ? "is" : "are"} kept.
        </p>
      ) : null}
      {doctor && (
        <div className="doctor">
          <p className="doctor-head">
            <span aria-hidden="true">✚</span> Doctor
          </p>
          <p>{doctor.hint}</p>
          {doctor.fix && (
            <>
              <SqlDiff before={sql} after={doctor.fix} />
              {onFix && (
                <button className="button primary small" onClick={() => onFix(doctor.fix!, doctor.rerun ?? doctor.fix!)}>
                  {doctor.fix_label ?? "Apply the fix"} & run
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function Results({ results }: { results: QueryResult[] }) {
  return (
    <div className="results">
      {results.map((result, i) => {
        const rows = result.rows.slice(0, SHOWN_ROWS);
        return (
          <div className="result" key={i}>
            <div className="result-head">
              <span className="status">{result.status}</span>
              {result.columns.length > 0 && (
                <span className="muted small">
                  {result.truncated || result.rows.length > SHOWN_ROWS
                    ? `showing ${rows.length} of ${result.row_total.toLocaleString()} rows`
                    : `${result.row_total.toLocaleString()} ${result.row_total === 1 ? "row" : "rows"}`}
                </span>
              )}
            </div>
            {result.columns.length > 0 && (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      {result.columns.map((c, ci) => (
                        <th key={ci} scope="col">
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
          </div>
        );
      })}
    </div>
  );
}
