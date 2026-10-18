import type { Counters, LogLine } from "../feed";

interface Props {
  log: LogLine[];
  counters: Counters;
  pending: number;
  onSkip: () => void;
}

export function EventLog({ log, counters, pending, onSkip }: Props) {
  const recent = log.slice(-120).reverse();
  return (
    <section className="panel log-panel" aria-label="Engine events">
      <header className="panel-head">
        <h2>Engine events</h2>
        {pending > 0 && (
          <button className="chip" onClick={onSkip} title="Apply every queued event now">
            {pending.toLocaleString()} queued · skip
          </button>
        )}
      </header>
      <dl className="counters">
        <div><dt>cache hits</dt><dd>{counters.hits.toLocaleString()}</dd></div>
        <div><dt>disk reads</dt><dd>{counters.misses.toLocaleString()}</dd></div>
        <div><dt>page writes</dt><dd>{counters.writes.toLocaleString()}</dd></div>
        <div><dt>splits</dt><dd>{counters.splits.toLocaleString()}</dd></div>
        <div><dt>log frames</dt><dd>{counters.logFrames.toLocaleString()}</dd></div>
        <div><dt>commits</dt><dd>{counters.commits.toLocaleString()}</dd></div>
      </dl>
      <ol className="log" aria-live="off">
        {recent.map((line) => (
          <li key={line.id} className={`log-line tone-${line.tone}`}>
            {line.text}
          </li>
        ))}
        {recent.length === 0 && <li className="muted small">Run a query to see what the engine does.</li>}
      </ol>
    </section>
  );
}
