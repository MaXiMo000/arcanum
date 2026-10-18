// The planner's journey for one statement, phase by phase in the order the optimizer runs (and the
// order PostgreSQL's planner works in): what it rewrote, where it moved filters, which access path it
// picked for each table and at what estimated cost, how it ordered the joins, and what it then ran.
import type { PlannerDecision, QueryResult } from "../api";
import { formatCount } from "../format";
import { plural } from "../learn/outcome";
import { flatten, type PgNode } from "./postgres";

interface Phase {
  id: PlannerDecision["phase"] | "parse" | "execute" | "advice";
  title: string;
  postgres: string;
  empty: string;
}

const PHASES: Phase[] = [
  { id: "parse", title: "Parse and bind", postgres: "raw_parser, then parse analysis", empty: "" },
  {
    id: "rewrite",
    title: "Rewrite subqueries",
    postgres: "pull_up_sublinks: EXISTS / IN become semi and anti joins",
    empty: "No subquery to pull up into a join.",
  },
  {
    id: "pushdown",
    title: "Push filters down",
    postgres: "distribute_qual_to_rels: each condition goes to the lowest node that can test it",
    empty: "Nothing to move: every condition already sits next to the only table it reads, or there is no join or aggregate to move it past.",
  },
  {
    id: "access",
    title: "Choose an access path per table",
    postgres: "set_base_rel_pathlists: cost a sequential scan and every usable index, keep the cheapest",
    empty: "No condition on any table that an index could use, so every table is read with a Seq Scan.",
  },
  {
    id: "join",
    title: "Choose the join order",
    postgres: "standard_join_search: dynamic programming over subsets of tables",
    empty: "Fewer than two tables in an inner join: nothing to order.",
  },
  {
    id: "index-only",
    title: "Check for index-only scans",
    postgres: "Index Only Scan when the index covers every column read",
    empty: "No secondary index scan in this plan.",
  },
  { id: "execute", title: "Execute", postgres: "the executor pulls rows through the plan, root first", empty: "" },
  {
    id: "advice",
    title: "What could be better",
    postgres: "what an experienced DBA would check next (the Advisor tab applies the fixes)",
    empty: "Nothing: the planner had what it needed and the plan has no known weak spots.",
  },
];

export function Planner({ result, root, onShow }: { result: QueryResult; root: PgNode; onShow: (node: number) => void }) {
  const nodes = flatten(root);
  const scans = nodes.filter((n) => n.family === "scan" || n.family === "index");
  return (
    <ol className="planner">
      {PHASES.map((phase, i) => {
        const decisions = result.planner.filter((d) => d.phase === phase.id);
        let body;
        if (phase.id === "parse") {
          body = (
            <p className="muted small">
              The SQL became a syntax tree; every table and column name was resolved against the catalog and every expression
              type-checked.{" "}
              {scans.length > 0 && (
                <>
                  Tables read: <strong>{[...new Set(scans.map((s) => s.relation).filter(Boolean))].join(", ")}</strong>.
                </>
              )}
              {result.planning_ms != null && <> Binding and planning together took {result.planning_ms.toFixed(2)} ms.</>}
            </p>
          );
        } else if (phase.id === "execute") {
          body = (
            <p className="muted small">
              {result.execution_ms != null
                ? `${plural(nodes.filter((n) => n.family !== "hash").length, "operator")} ran in ${result.execution_ms.toFixed(2)} ms and returned ${result.row_total.toLocaleString()} row${result.row_total === 1 ? "" : "s"}${root.est !== null ? ` (the planner expected about ${formatCount(Math.round(root.est))})` : ""}.`
                : "EXPLAIN without ANALYZE stops here: the plan is shown but never run."}
            </p>
          );
        } else if (phase.id === "advice") {
          const issues = result.advice.filter((a) => a.level !== "good");
          body = issues.length ? (
            issues.map((a, j) => (
              <div key={j} className={`decision advice-${a.level}`}>
                <div className="decision-head">
                  <strong>{a.title}</strong>
                  {a.node !== null && (
                    <button className="link small" onClick={() => onShow(a.node!)}>
                      show in flow →
                    </button>
                  )}
                </div>
                <p className="muted small">{a.detail}</p>
                {a.estimate && (
                  <p className="small">
                    With <code>{a.fix?.sql}</code> the planner estimates cost {formatCount(a.estimate.before)} → <strong>{formatCount(a.estimate.after)}</strong> ({a.estimate.path}).
                  </p>
                )}
                {!a.estimate && a.fix && (
                  <p className="small">
                    Fix: <code>{a.fix.sql}</code>
                  </p>
                )}
              </div>
            ))
          ) : (
            <p className="muted small">{phase.empty}</p>
          );
        } else if (decisions.length === 0) {
          body = <p className="muted small">{phase.empty}</p>;
        } else {
          body = decisions.map((d, j) => (
            <div key={j} className="decision">
              <div className="decision-head">
                <strong>{d.title}</strong>
                {d.node !== null && (
                  <button className="link small" onClick={() => onShow(d.node!)}>
                    show in flow →
                  </button>
                )}
              </div>
              {d.detail && <p className="muted small">{d.detail}</p>}
              {d.candidates.length > 0 && <CandidateTable candidates={d.candidates} />}
            </div>
          ));
        }
        const active =
          decisions.length > 0 ||
          phase.id === "parse" ||
          (phase.id === "execute" && result.execution_ms != null) ||
          (phase.id === "advice" && result.advice.some((a) => a.level !== "good"));
        return (
          <li key={phase.id} className={`phase ${active ? "active" : ""}`} style={{ animationDelay: `${i * 50}ms` }}>
            <span className="phase-number" aria-hidden="true">
              {i + 1}
            </span>
            <div className="phase-body">
              <h4>{phase.title}</h4>
              <p className="phase-pg">In PostgreSQL: {phase.postgres}</p>
              {body}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function CandidateTable({ candidates }: { candidates: PlannerDecision["candidates"] }) {
  const max = Math.max(1, ...candidates.map((c) => c.cost ?? 0));
  return (
    <ul className="candidates">
      {candidates.map((c, i) => (
        <li key={i} className={`candidate ${c.chosen ? "chosen" : ""} ${c.cost === null ? "unusable" : ""}`}>
          <div className="candidate-head">
            <span className="candidate-mark" aria-hidden="true">
              {c.chosen ? "✓" : c.cost === null ? "–" : "✕"}
            </span>
            <span className="candidate-path">{c.path}</span>
            <span className="candidate-cost">{c.cost !== null ? `cost ${formatCount(c.cost)}` : "not usable"}</span>
          </div>
          {c.cost !== null && (
            <span className="candidate-bar" aria-hidden="true">
              <i style={{ width: `${Math.max(2, (c.cost / max) * 100)}%` }} />
            </span>
          )}
          <p className="candidate-why">
            {c.why}
            {c.rows !== null && c.cost !== null && ` · about ${formatCount(c.rows)} rows`}
            {c.chosen && " · cheapest, chosen"}
          </p>
        </li>
      ))}
    </ul>
  );
}
