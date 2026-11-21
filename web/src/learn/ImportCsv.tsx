// CSV import: pick a file, see what the engine will make of it (new table and inferred types, or rows for an
// existing one), then load it with one CREATE TABLE and one INSERT that run like any statement.
import { useEffect, useState } from "react";
import { api, ApiError, type ImportPreview, type TableInfo } from "../api";
import { formatCount } from "../format";

interface Props {
  sessionId: string | null;
  maxBytes: number | null;
  tables: TableInfo[];
  /** Runs the import; resolves to an error message, or null when it ran. */
  onImport: (request: { table: string; csv: string; header: boolean }) => Promise<string | null>;
  onClose: () => void;
}

/** A file name as a table name the import accepts: lowercase letters, digits and _. */
function tableName(file: string): string {
  const base = file.replace(/\.[^.]*$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return (/^[a-z_]/.test(base) ? base : `t_${base}`).slice(0, 63) || "imported";
}

export function ImportCsv({ sessionId, maxBytes, tables, onImport, onClose }: Props) {
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [table, setTable] = useState("");
  const [header, setHeader] = useState(true);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const choose = async (picked: File | undefined) => {
    setPreview(null);
    setError(null);
    if (!picked) return setFile(null);
    if (maxBytes !== null && picked.size > maxBytes) {
      setFile(null);
      return setError(`${picked.name} is ${formatCount(Math.round(picked.size / 1024))} KB; imports are limited to ${formatCount(Math.round(maxBytes / 1024))} KB.`);
    }
    setFile({ name: picked.name, text: await picked.text() });
    setTable(tableName(picked.name));
  };

  // Preview whenever the file, the table or the header setting changes.
  useEffect(() => {
    if (!sessionId || !file || !/^[a-z_][a-z0-9_]*$/.test(table)) {
      setPreview(null);
      return;
    }
    let current = true;
    api
      .previewImport(sessionId, { table, csv: file.text, header })
      .then((p) => current && (setPreview(p), setError(null)))
      .catch((e) => current && (setPreview(null), setError(e instanceof ApiError ? e.message : String(e))));
    return () => {
      current = false;
    };
  }, [sessionId, file, table, header]);

  const run = async () => {
    if (!file) return;
    setRunning(true);
    const failed = await onImport({ table, csv: file.text, header });
    setRunning(false);
    if (failed) setError(failed);
  };

  return (
    <section className="import-csv" aria-label="Import a CSV file">
      <div className="import-row">
        <input className="import-file" type="file" accept=".csv,.tsv,.txt,text/csv,text/plain" aria-label="CSV file"
          onChange={(e) => void choose(e.target.files?.[0])} />
        <button className="button ghost small" onClick={onClose} aria-label="Close the import">
          ✕
        </button>
      </div>
      {file && (
        <div className="import-row">
          <label>
            <span>Table</span>
            <input value={table} onChange={(e) => setTable(e.target.value.toLowerCase())} maxLength={63} spellCheck={false}
              list="import-tables" aria-invalid={!/^[a-z_][a-z0-9_]*$/.test(table)} />
            <datalist id="import-tables">
              {tables.map((t) => (
                <option key={t.name} value={t.name} />
              ))}
            </datalist>
          </label>
          <label className="import-check">
            <input type="checkbox" checked={header} onChange={(e) => setHeader(e.target.checked)} /> First row names the columns
          </label>
        </div>
      )}
      {error && (
        <p className="error-box small" role="alert">
          {error}
        </p>
      )}
      {preview && (
        <>
          <p className="small">
            {preview.create ? (
              <>
                Creates <b>{preview.table}</b> with these columns and inserts {formatCount(preview.rows_total)} rows:
              </>
            ) : (
              <>
                Adds {formatCount(preview.rows_total)} rows to the existing <b>{preview.table}</b>, filling:
              </>
            )}
          </p>
          <div className="import-columns">
            {preview.columns.map((c) => (
              <span key={c.name} className="chip">
                {c.name} <span className="muted">{c.type}</span>
              </span>
            ))}
          </div>
          <div className="import-sample">
            <table>
              <tbody>
                {preview.sample.map((row, i) => (
                  <tr key={i}>
                    {row.map((v, j) => (
                      <td key={j}>{v === "" ? <span className="muted">NULL</span> : v}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button className="button primary small" onClick={() => void run()} disabled={running || !preview.rows_total}>
            {running ? "Importing…" : `Import ${formatCount(preview.rows_total)} rows`}
          </button>
        </>
      )}
      {!file && !error && (
        <p className="muted small">
          A CSV file with a header row. New tables get a column type per column (INTEGER, REAL, BOOLEAN, DATE, TIMESTAMP or
          TEXT); an existing table is filled by column name. Empty fields become NULL.
        </p>
      )}
    </section>
  );
}
