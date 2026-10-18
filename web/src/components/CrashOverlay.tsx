import { useEffect, useRef } from "react";
import type { CrashSummary } from "../api";
import type { CrashPhase } from "../feed";

interface Props {
  phase: CrashPhase;
  summary: CrashSummary | null;
  error: string | null;
  onClose: () => void;
}

const ORDER = ["writing", "crashed", "recovering", "done"] as const;

export function CrashOverlay({ phase, summary, error, onClose }: Props) {
  const dialog = useRef<HTMLDivElement>(null);
  const finished = (phase.phase === "done" || phase.phase === "idle") && (summary !== null || error !== null);
  const reached = phase.phase === "idle" ? (finished ? 4 : 0) : ORDER.indexOf(phase.phase) + 1;

  useEffect(() => {
    dialog.current?.focus();
  }, []);

  useEffect(() => {
    if (!finished) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finished, onClose]);

  const step = (index: number, title: string, detail: string) => (
    <li className={`crash-step ${reached > index ? "reached" : ""} ${reached === index + 1 && !finished ? "current" : ""}`}>
      <span className="step-dot" aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </li>
  );

  return (
    <div className="overlay" role="presentation">
      <div className="crash-dialog" role="dialog" aria-modal="true" aria-labelledby="crash-title" tabIndex={-1} ref={dialog}>
        <h2 id="crash-title">Pulling the plug</h2>
        <ol className="crash-steps">
          {step(0, "Writing", phase.phase === "writing" && phase.batches
            ? `${phase.batches} transactions of 25 rows committed so far…`
            : "Committing transactions of 25 rows each.")}
          {step(1, "Power cut", "The engine process is killed in the middle of the next transaction.")}
          {step(2, "Replaying the log", phase.phase === "recovering"
            ? `Replaying ${phase.frames.toLocaleString()} committed log frames into the data file.`
            : "A fresh engine opens the database and replays every committed transaction from the log.")}
          {step(3, "Checking the data", "Every acknowledged commit must be back; the interrupted one, all or nothing.")}
        </ol>
        {error && <p className="error-box" role="alert">{error}</p>}
        {finished && summary && (
          <div className={`crash-result ${summary.consistent === false ? "bad" : "good"}`} aria-live="polite">
            {summary.acknowledged_rows !== undefined ? (
              <>
                <p className="crash-verdict">
                  {summary.consistent ? "No committed data lost." : "Inconsistent state detected."}
                </p>
                <dl>
                  <div><dt>rows acknowledged before the crash</dt><dd>{summary.acknowledged_rows.toLocaleString()}</dd></div>
                  <div><dt>rows after recovery</dt><dd>{summary.rows_after_recovery?.toLocaleString()}</dd></div>
                  <div><dt>log frames replayed</dt><dd>{summary.recovered_frames.toLocaleString()}</dd></div>
                  <div>
                    <dt>interrupted transaction ({summary.in_flight_rows} rows)</dt>
                    <dd>{summary.in_flight_committed ? "had committed: kept whole" : "never committed: gone whole"}</dd>
                  </div>
                </dl>
              </>
            ) : (
              <p className="crash-verdict">Recovered, replaying {summary.recovered_frames.toLocaleString()} log frames.</p>
            )}
          </div>
        )}
        <div className="dialog-actions">
          <button className="button" onClick={onClose} disabled={!finished}>
            {finished ? "Close" : "Recovering…"}
          </button>
        </div>
      </div>
    </div>
  );
}
