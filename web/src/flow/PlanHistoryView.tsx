// The plans one query has had, run by run, and what changed between two of them.
import { useState } from "react";
import { formatCount } from "../format";
import { diffLines, samePlan, type PlanRun } from "./planHistory";

const time = (at: number) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function PlanHistoryView({ runs }: { runs: PlanRun[] }) {
  // Compare the latest run with the last one whose plan differs (or simply the one before).
  const last = runs.length - 1;
  let changed = last - 1;
  while (changed >= 0 && samePlan(runs[changed]!, runs[last]!)) changed--;
  const [pair, setPair] = useState<[number, number] | null>(null);
  const [a, b] = pair ?? [changed >= 0 ? changed : Math.max(0, last - 1), last];
  if (runs.length < 2) {
    return (
      <section className="plan-history" aria-label="Plan history">
        <h3>Plan history</h3>
        <p className="muted small">
          Run this query again after a change (CREATE INDEX, ANALYZE, more rows) to compare its plans here.
        </p>
      </section>
    );
  }
  const diff = diffLines(runs[a]!.lines, runs[b]!.lines);
  const plans = new Set(runs.map((r) => r.lines.join("\n"))).size;

  return (
    <section className="plan-history" aria-label="Plan history">
      <h3>
        Plan history <span className="muted small">{runs.length} runs, {plans} different plan{plans === 1 ? "" : "s"}</span>
      </h3>
      <div className="plan-history-table">
        <table>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Plan</th>
              <th scope="col">Est. cost</th>
              <th scope="col">Pages</th>
              <th scope="col">Time</th>
              <th scope="col">Compare</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run, i) => {
              const fresh = i > 0 && !samePlan(run, runs[i - 1]!);
              return (
                <tr key={run.at + i} className={i === a || i === b ? "picked" : ""}>
                  <td>
                    #{i + 1} <span className="muted small">{time(run.at)}</span>
                  </td>
                  <td>{i === 0 ? "first" : fresh ? <b className="plan-changed">changed</b> : <span className="muted">same</span>}</td>
                  <td>{run.cost === null ? "—" : formatCount(Math.round(run.cost))}</td>
                  <td>{run.pages === null ? "—" : formatCount(run.pages)}</td>
                  <td>{run.ms === null ? "—" : `${run.ms.toFixed(1)} ms`}</td>
                  <td className="plan-pick">
                    <button className={`chip ${i === a ? "active" : ""}`} onClick={() => setPair([i, b === i ? a : b])}
                      aria-label={`Compare from run ${i + 1}`}>
                      A
                    </button>
                    <button className={`chip ${i === b ? "active" : ""}`} onClick={() => setPair([a === i ? b : a, i])}
                      aria-label={`Compare to run ${i + 1}`}>
                      B
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="small">
        Run #{a + 1} (A) against run #{b + 1} (B){diff.every((d) => d.kind === "same") ? ": the same plan." : ":"}
      </p>
      {!diff.every((d) => d.kind === "same") && (
        <pre className="plan-diff" aria-label={`Plan of run ${a + 1} against run ${b + 1}`}>
          {diff.map((d, i) => (
            <span key={i} className={d.kind}>
              {d.kind === "removed" ? "- " : d.kind === "added" ? "+ " : "  "}
              {d.text}
              {"\n"}
            </span>
          ))}
        </pre>
      )}
    </section>
  );
}
