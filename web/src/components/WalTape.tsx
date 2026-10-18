import type { WalFrame } from "../feed";

interface Props {
  frames: WalFrame[];
  committedOnDisk: number;
  checkpointAt: number | null;
  flashMs: number;
}

export function WalTape({ frames, committedOnDisk, checkpointAt, flashMs }: Props) {
  const justCheckpointed = checkpointAt !== null && performance.now() - checkpointAt < flashMs * 2;
  const pending = frames.filter((f) => !f.committed).length;
  return (
    <section className="panel" aria-label="Write-ahead log">
      <header className="panel-head">
        <h2>Write-ahead log</h2>
        <span className="muted small">
          {frames.length ? `${frames.length} frames${pending ? ` · ${pending} not yet synced` : ""}` : `${committedOnDisk} committed frames`}
        </span>
      </header>
      <div className={`wal-tape ${justCheckpointed ? "checkpointed" : ""}`} aria-live="polite">
        {frames.length === 0 && (
          <p className="muted small wal-empty">
            {justCheckpointed
              ? "Checkpoint: every logged page was copied into the data file and the log was emptied."
              : "Every change is written here first. Commits make it durable; checkpoints copy it into the data file."}
          </p>
        )}
        {frames.map((f) => (
          <span
            key={f.id}
            className={`frame ${f.commit ? "commit" : ""} ${f.committed ? "synced" : "pending"}`}
            title={f.commit ? "commit frame" : `page ${f.page}`}
          >
            {f.commit ? "✓" : f.page}
          </span>
        ))}
      </div>
      <footer className="legend">
        <span><i className="swatch frame-pending" /> written, not synced</span>
        <span><i className="swatch frame-synced" /> durable</span>
        <span><i className="swatch frame-commit" /> commit frame</span>
      </footer>
    </section>
  );
}
