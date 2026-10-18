// The query advisor: what the engine thinks of the last statement, with one-click fixes and a
// before/after measurement of each fix.
import { useState, type CSSProperties } from "react";
import type { Advice, AdviceFix, QueryResult } from "../api";
import { formatCount } from "../format";
import { flatten, toPostgres } from "../flow/postgres";
import { RichText } from "../learn/RichText";
import { pagesRead, type Outcome } from "../learn/outcome";

export interface Comparison {
  label: string;
  before: Outcome;
  after: Outcome;
}

const PENALTY = { warn: 25, tip: 8, good: 0 } as const;
const LEVEL_NAME = { warn: "Problem", tip: "Tip", good: "Good" } as const;

/** 0-100 from the findings: problems cost most, tips a little. */
export function healthScore(advice: Advice[]) {
  return Math.max(0, 100 - advice.reduce((sum, a) => sum + PENALTY[a.level], 0));
}

function grade(score: number) {
  return score >= 95 ? "A" : score >= 80 ? "B" : score >= 60 ? "C" : score >= 40 ? "D" : "E";
}

interface Props {
  outcome: Outcome | null;
  comparison: Comparison | null;
  onApply: (fix: AdviceFix, result: QueryResult) => Promise<void>;
  busy: boolean;
}

export function Advisor({ outcome, comparison, onApply, busy }: Props) {
  const [applying, setApplying] = useState<string | null>(null);
  if (!outcome) {
    return (
      <div className="empty-state">
        <p className="empty-title">The advisor is waiting</p>
        <p>
          Run any statement. The advisor reads its plan and real row counts, then points out missing indexes, wasted table lookups,
          accidental cartesian products, NULL traps and more, each with a fix you can apply and measure.
        </p>
      </div>
    );
  }
  const findings = outcome.results.flatMap((result) => result.advice.map((advice) => ({ advice, result })));
  const score = healthScore(findings.map((f) => f.advice));
  const counts = { warn: 0, tip: 0, good: 0 };
  findings.forEach((f) => counts[f.advice.level]++);

  const apply = async (fix: AdviceFix, result: QueryResult, key: string) => {
    setApplying(key);
    try {
      await onApply(fix, result);
    } finally {
      setApplying(null);
    }
  };

  return (
    <div className="advisor">
      <div className="health">
        <div className={`health-ring grade-${grade(score)}`} style={{ "--score": score } as CSSProperties}>
          <span>{grade(score)}</span>
        </div>
        <div>
          <p className="eyebrow">Query health</p>
          <p className="health-line">
            {counts.warn ? `${counts.warn} problem${counts.warn > 1 ? "s" : ""}` : "No problems"}
            {counts.tip ? ` · ${counts.tip} tip${counts.tip > 1 ? "s" : ""}` : ""}
            {counts.good ? ` · ${counts.good} good sign${counts.good > 1 ? "s" : ""}` : ""}
          </p>
          <p className="muted small">Judged from the plan the optimizer chose and the rows each operator really produced.</p>
        </div>
      </div>

      {comparison && <CompareCard comparison={comparison} />}

      {findings.length === 0 && (
        <p className="callout success">
          Nothing to improve: this runs the way an experienced engineer would write it. Try a slow query from the playground
          examples to see the advisor at work.
        </p>
      )}
      <ul className="advice-list">
        {findings.map(({ advice, result }, i) => {
          const key = `${i}-${advice.code}`;
          return (
            <li key={key} className={`advice advice-${advice.level}`}>
              <span className={`advice-badge ${advice.level}`}>{LEVEL_NAME[advice.level]}</span>
              <strong className="advice-title">{advice.title}</strong>
              <p>
                <RichText text={advice.detail} />
              </p>
              {advice.estimate && (
                <div className="estimate" title="The optimizer planned the query as if the index existed, without creating it">
                  <span className="muted">Planner estimate</span>
                  <span className="estimate-cost">cost {formatCount(advice.estimate.before)}</span>
                  <span aria-hidden="true">→</span>
                  <span className="estimate-cost better">cost {formatCount(advice.estimate.after)}</span>
                  <span className="better">−{Math.round((1 - advice.estimate.after / advice.estimate.before) * 100)}%</span>
                  <span className="estimate-path">via {advice.estimate.path}</span>
                </div>
              )}
              {advice.fix && (
                <div className="advice-fix">
                  {advice.fix.kind === "rewrite" && advice.fix.original ? (
                    <SqlDiff before={advice.fix.original} after={advice.fix.sql} />
                  ) : (
                    <pre className="fix-sql">{advice.fix.sql}</pre>
                  )}
                  <button className="button primary small" disabled={busy || applying !== null} onClick={() => void apply(advice.fix!, result, key)}>
                    {applying === key ? "Measuring…" : advice.fix.kind === "run" ? `Apply & compare: ${advice.fix.label}` : `${advice.fix.label} & compare`}
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function scans(results: QueryResult[]): string[] {
  return results.flatMap((r) =>
    r.plan ? flatten(toPostgres(r.plan)).filter((n) => n.family === "scan" || n.family === "index").map((n) => n.name) : [],
  );
}

function CompareCard({ comparison }: { comparison: Comparison }) {
  const { before, after, label } = comparison;
  const metrics = [
    { name: "Pages read", a: pagesRead(before.stats), b: pagesRead(after.stats) },
    { name: "Tree nodes visited", a: before.stats.nodes_visited, b: after.stats.nodes_visited },
    { name: "Time (ms)", a: before.stats.elapsed_ms, b: after.stats.elapsed_ms, decimals: 2 },
  ];
  return (
    <section className="compare" aria-label="Before and after">
      <p className="eyebrow">Measured: {label}</p>
      <table>
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col">Before</th>
            <th scope="col">After</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => {
            const change = m.a > 0 ? (m.b - m.a) / m.a : 0;
            return (
              <tr key={m.name}>
                <th scope="row">{m.name}</th>
                <td>{m.a.toFixed(m.decimals ?? 0)}</td>
                <td>{m.b.toFixed(m.decimals ?? 0)}</td>
                <td className={change < -0.02 ? "better" : change > 0.02 ? "worse" : "muted"}>
                  {Math.abs(change) < 0.02 ? "same" : `${change < 0 ? "−" : "+"}${Math.round(Math.abs(change) * 100)}%`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="compare-paths small">
        <span className="muted">Access path:</span> {scans(before.results).join(", ") || "none"} <span aria-hidden="true">→</span>{" "}
        <strong>{scans(after.results).join(", ") || "none"}</strong>
      </p>
      <p className="muted small">Time on a tiny demo table is noisy; pages and nodes are exact.</p>
    </section>
  );
}

/** The changed middle of two texts, with a little context either side. */
export function SqlDiff({ before, after }: { before: string; after: string }) {
  let start = 0;
  while (start < before.length && before[start] === after[start]) start++;
  let end = 0;
  while (end < before.length - start && end < after.length - start && before[before.length - 1 - end] === after[after.length - 1 - end]) end++;
  const context = 24;
  const lead = before.slice(Math.max(0, start - context), start);
  const tail = before.slice(before.length - end, before.length - end + context);
  return (
    <pre className="sql-diff" aria-label={`Change: ${before} to ${after}`}>
      {start > context && "…"}
      {lead}
      <del>{before.slice(start, before.length - end)}</del>
      <ins>{after.slice(start, after.length - end)}</ins>
      {tail}
      {end > context && "…"}
    </pre>
  );
}
