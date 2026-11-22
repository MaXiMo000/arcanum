// A guided tour of a lab: a few experiments in order, each set up and run by one button, with what to
// look for. Steps can be taken in any order; done ones are ticked.
import { useState } from "react";

export interface TourStep {
  title: string;
  /** What to look for once it has run. */
  body: string;
  run: () => Promise<unknown> | void;
}

export function LabTour({ steps, busy }: { steps: TourStep[]; busy: boolean }) {
  const [done, setDone] = useState<Set<number>>(new Set());
  const [running, setRunning] = useState<number | null>(null);
  const next = steps.findIndex((_, i) => !done.has(i));

  const take = async (i: number) => {
    setRunning(i);
    try {
      await steps[i]!.run();
      setDone((d) => new Set(d).add(i));
    } finally {
      setRunning(null);
    }
  };

  return (
    <details className="lab-tour" open>
      <summary>
        <b>Guided tour</b>
        <span className="muted small">
          {done.size} of {steps.length} done
        </span>
      </summary>
      <ol>
        {steps.map((step, i) => (
          <li key={step.title} className={`${done.has(i) ? "done" : ""} ${i === next ? "next" : ""}`}>
            <div>
              <b>{step.title}</b>
              <p className="muted small">{step.body}</p>
            </div>
            <button className={`button small ${i === next ? "primary" : "ghost"}`} disabled={busy || running !== null}
              onClick={() => void take(i)}>
              {running === i ? "Running…" : done.has(i) ? "Again" : "Run it"}
            </button>
          </li>
        ))}
      </ol>
    </details>
  );
}
