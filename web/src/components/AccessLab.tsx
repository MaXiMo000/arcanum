// Three access methods on one table: a B+tree, a hash index and a column store, asked the same questions,
// each measured by the pages it reads.
import { useState } from "react";
import { api, type AccessResult } from "../api";
import { formatCount } from "../format";
import { useLab } from "../useLab";
import { BarCompare } from "./BarCompare";

const ENGINES = [
  { key: "btree", label: "B+tree", className: "eng-btree" },
  { key: "hash", label: "Hash index", className: "eng-lsm" },
  { key: "column", label: "Column store", className: "eng-third" },
] as const;

const WHY: Record<string, string> = {
  point: "The hash index computes the bucket and reads it; the B+tree descends a few levels; the column store reads one page per column.",
  range: "The B+tree and the column store keep rows in id order and read only the pages that hold the range. Hashing scatters neighbouring ids, so the hash index reads every bucket.",
  sum: "Only the column store can read the price column alone; the others read every row, all five columns of it.",
  filter: "The category values come in runs, so each column-store page records which categories it holds (its min and max) and the store skips the rest. Run-length encoding keeps the whole column in very few pages.",
  insert: "The B+tree and the column store append near the end; new keys hash to buckets all over the index, so it rewrites almost all of them.",
};

export function AccessLab({ sessionId }: { sessionId: string | null }) {
  const [rows, setRows] = useState(5000);
  const { result, running, error, run } = useLab(sessionId, api.accessLab);

  return (
    <div className="engine-lab">
      <header className="lab-head">
        <h2>Access methods</h2>
        <p className="muted">
          One table of orders stored three ways: a B+tree keyed by id, a hash index on id (extendible hashing) and a
          column store (each column in its own compressed pages). Ask each the same questions and count the pages it reads.
        </p>
      </header>

      <section className="lab-controls" aria-label="Table size">
        <label>
          <span>
            Rows <b>{formatCount(rows)}</b>
          </span>
          <input type="range" min={1000} max={20000} step={1000} value={rows} onChange={(e) => setRows(Number(e.target.value))} />
        </label>
        <button className="button primary" onClick={() => void run({ rows })} disabled={running || !sessionId}>
          {running ? (
            <>
              <span className="spinner" aria-hidden="true" /> Loading three structures…
            </>
          ) : (
            "Load and ask"
          )}
        </button>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}
      {!result && !error && <p className="muted lab-empty">Pick a table size and load it into all three.</p>}
      {result && <AccessReport result={result} />}
    </div>
  );
}

function AccessReport({ result }: { result: AccessResult }) {
  const { details } = result;
  return (
    <div className="lab-report">
      <p className="lab-takeaway">
        No structure wins everything. With {formatCount(result.rows)} rows the hash index grew to {formatCount(details.hash_depth)} bits of
        directory ({formatCount(details.hash_splits)} bucket splits) and the column store fits the category column in{" "}
        {details.column_pages.category} page{details.column_pages.category === 1 ? "" : "s"} against {details.column_pages.note} for the
        notes.
      </p>
      <div className="amp-grid">
        {result.queries.map((q) => (
          <div key={q.key} className="access-card">
            <BarCompare
              title={q.title}
              hint={`${q.about} Pages ${q.per === "batch" ? "written" : "read per query"}.`}
              best="lowest"
              bars={ENGINES.map((e) => ({ key: e.key, label: e.label, className: e.className, value: q.pages[e.key] ?? 0 }))}
            />
            <p className="muted small access-why">{WHY[q.key]}</p>
          </div>
        ))}
        <BarCompare
          title="Size on disk"
          hint="Pages each structure takes for the same rows."
          best="lowest"
          bars={ENGINES.map((e) => ({
            key: e.key,
            label: e.label,
            className: e.className,
            value: result.engines[e.key].pages,
            note: `${formatCount(Math.round(result.engines[e.key].bytes / 1024))} KB, loaded in ${formatCount(result.load_ms[e.key])} ms`,
          }))}
        />
      </div>
    </div>
  );
}
